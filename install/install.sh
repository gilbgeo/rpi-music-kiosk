#!/bin/bash

# --- Vérification système ---
echo "🔍 Vérification du système…"

OS_NAME=$(grep '^NAME=' /etc/os-release | cut -d= -f2 | tr -d '"')
OS_VERSION=$(grep '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
ARCHITECTURE=$(uname -m)

if [[ "$OS_NAME" != "Raspbian GNU/Linux" && "$OS_NAME" != "Debian GNU/Linux" ]]; then
  echo "❌ Ce script est prévu pour Raspberry Pi OS (Raspbian). Système détecté : $OS_NAME"
  exit 1
fi

if [[ "$OS_VERSION" != "12" ]]; then
  echo "❌ Version non supportée : Raspberry Pi OS $OS_VERSION détecté. Seule la version 12 (bookworm) est supportée."
  exit 1
fi

if [[ "$ARCHITECTURE" != "armv7l" ]]; then
  echo "❌ Architecture non supportée : $ARCHITECTURE. Ce script ne fonctionne que sur Raspberry Pi OS 32 bits."
  exit 1
fi

echo "✅ Environnement valide : Raspberry Pi OS 12 (32 bits) détecté."

# --- Mise à jour ---
echo "🛠️ Mise à jour du système"
sudo apt update && sudo apt upgrade -y

# --- Installation des paquets ---
echo "📦 Installation des paquets système"
sudo apt install -y python3-pip \
  python3-tk \
  python3-pil \
  python3-pil.imagetk \
  python3-gi \
  python3-requests \
  python3-dbus \
  gir1.2-gstreamer-1.0 \
  gir1.2-gtk-3.0 \
  gstreamer1.0-tools \
  gstreamer1.0-alsa \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  libdiscid0 \
  python3-psutil \
  python3-setuptools \
  python3-wheel \
  build-essential \
  libasound2 \
  libdiscid-dev \
  ffmpeg \
  python3-numpy \
  libopenblas0

sudo apt install -y --no-install-recommends xserver-xorg xinit x11-xserver-utils xterm openbox

# --- Installation des bibliothèques Python ---
echo "🐍 Installation des dépendances Python via pip"
pip3 install musicbrainzngs python-libdiscid pydbus Pillow shazamio --break-system-packages

# --- Spotify Daemon ---
echo "🎵 Installation de Spotifyd"
sudo rm -f /usr/local/bin/spotifyd
sudo apt remove -y spotifyd || true
wget https://github.com/Spotifyd/spotifyd/releases/download/v0.4.1/spotifyd-linux-armv7-full.tar.gz
 tar xvf spotifyd-linux-armv7-full.tar.gz
sudo mv spotifyd /usr/local/bin/spotifyd
sudo chmod +x /usr/local/bin/spotifyd
mkdir -p /home/player/.config/spotifyd
cat << EOF > /home/player/.config/spotifyd/spotifyd.conf
[global]
backend = "alsa"
device_name = "Kiosk"
device = "hw:1,0"
EOF

# --- Bluetooth ---
echo "📶 Installation du Bluetooth"
sudo apt install -y --no-install-recommends bluez-tools bluez-alsa-utils

read -p "💡 Utilisez-vous un dongle Bluetooth externe ? (o/n) " USE_BT_DONGLE

sudo tee /etc/systemd/system/bt-agent@.service >/dev/null <<'EOF'
[Unit]
Description=Bluetooth Agent
Requires=bluetooth.service
After=bluetooth.service

[Service]
ExecStartPre=/usr/bin/bluetoothctl discoverable on
ExecStartPre=/bin/hciconfig %I piscan
ExecStartPre=/bin/hciconfig %I sspmode 1
ExecStart=/usr/bin/bt-agent --capability=NoInputNoOutput
RestartSec=5
Restart=always
KillSignal=SIGUSR1

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/bluetooth/main.conf >/dev/null <<'EOF'
[General]
Class = 0x200414
DiscoverableTimeout = 0

[Policy]
AutoEnable=true
EOF

sudo tee /usr/local/bin/bluetooth-udev >/dev/null <<'EOF'
#!/bin/bash
if [[ ! $NAME =~ ^"([0-9A-F]{2}[:-]){5}([0-9A-F]{2})"$ ]]; then exit 0; fi

action=$(expr "$ACTION" : "\([a-zA-Z]\+\).*")

if [ "$action" = "add" ]; then
    bluetoothctl discoverable off
fi

if [ "$action" = "remove" ]; then
    bluetoothctl discoverable on
fi
EOF

sudo chmod 755 /usr/local/bin/bluetooth-udev

sudo tee /etc/udev/rules.d/99-bluetooth-udev.rules >/dev/null <<'EOF'
SUBSYSTEM=="input", GROUP="input", MODE="0660"
KERNEL=="input[0-9]*", RUN+="/usr/local/bin/bluetooth-udev"
EOF

sudo systemctl daemon-reload
sudo systemctl enable bt-agent@hci0.service

# --- Nom d'hôte ---
echo "📛 Changement du nom d'hôte en 'Kiosk'"
sudo hostnamectl set-hostname --pretty "Kiosk"

# --- Configuration ALSA ---
echo "🔊 Création du fichier /etc/asound.conf"
sudo tee /etc/asound.conf > /dev/null <<'EOF'
defaults.pcm.card 1
defaults.ctl.card 1

pcm.!default {
  type asym
  playback.pcm "LoopAndReal"
  capture.pcm "hw:1,0"
}

pcm.looprec {
    type hw
    card "Loopback"
    device 0
    subdevice 0
}

pcm.LoopAndReal {
  type plug
  slave.pcm mdev
  route_policy "duplicate"
}

pcm.mdev {
  type multi
  slaves.a.pcm pcm.MixReale
  slaves.a.channels 2
  slaves.b.pcm pcm.MixLoopback
  slaves.b.channels 2
  bindings.0.slave a
  bindings.0.channel 0
  bindings.1.slave a
  bindings.1.channel 1
  bindings.2.slave b
  bindings.2.channel 0
  bindings.3.slave b
  bindings.3.channel 1
}

pcm.MixReale {
  type dmix
  ipc_key 1024
  slave {
    pcm "hw:1,0"
    rate 48000
    periods 128
    period_time 0
    period_size 1024
    buffer_size 8192
  }
}

pcm.MixLoopback {
  type dmix
  ipc_key 1025
  slave {
    pcm "hw:Loopback,0,0"
    rate 48000
    periods 128
    period_time 0
    period_size 1024
    buffer_size 8192
  }
}
EOF

# --- Autologin pour l'utilisateur player ---
echo "🔐 Configuration de l'autologin pour l'utilisateur player"
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/override.conf > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin player --noclear %I \$TERM
EOF

# --- ~/.profile pour démarrer X automatiquement ---
echo "🧾 Ajout du lancement automatique de startx dans ~/.profile"
cat <<'EOF' >> /home/player/.profile

# lancer X automatiquement sur tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec startx
fi
EOF

# --- ~/.xinitrc pour démarrer le script Python avec Openbox et configuration utile ---
echo "📺 Création de ~/.xinitrc avec démarrage de Openbox et du script kiosk"
sudo tee /home/player/.xinitrc > /dev/null <<EOF
#!/bin/sh

# Petite pause pour s'assurer que tout est prêt
sleep 1

# Cache le pointeur de souris après 0.5s
unclutter -idle 0.5 -root &

# Désactive économiseur et DPMS
xset s off
xset -dpms
xset s noblank

# Lance Openbox (ou openbox-session si tu préfères)
openbox-session &

# Petite pause pour que le WM soit fully up
sleep 1

# *** Lance ton appli Kiosk au premier plan ***
exec python3 /home/player/Kiosk/kiosk.py
EOF

# --- Ajout de l'utilisateur player à audio pour snd-aloop sans sudo ---
echo "🔉 Ajout de l'utilisateur 'player' au groupe audio pour l'accès à snd-aloop"
sudo usermod -aG audio player

# --- Écran MHS35 ---
read -p "🖥️ Souhaitez-vous installer l'écran MHS35 ? (o/n) " INSTALL_MHS35
if [[ "$INSTALL_MHS35" == "o" || "$INSTALL_MHS35" == "O" ]]; then
  git clone https://github.com/goodtft/LCD-show
  cd LCD-show/
  sudo sed -i '/^sudo echo "hdmi/d' MHS35-show
  sudo sed -i '/^sudo reboot/d' MHS35-show
  sudo ./MHS35-show
fi

# --- Ajout du dtoverlay=iqaudio-dac ---
echo "🔧 Ajout de dtoverlay=iqaudio-dac dans /boot/firmware/config.txt"
sudo sed -i '1i dtoverlay=iqaudio-dac' /boot/firmware/config.txt
if [[ "$USE_BT_DONGLE" == "o" || "$USE_BT_DONGLE" == "O" ]]; then
  echo "🔧 Configuration pour dongle Bluetooth externe"
  echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
  sudo rfkill unblock all
  for hci in $(hciconfig | grep -o '^hci[0-9]'); do
    sudo hciconfig "$hci" up
  done
fi
sudo reboot
