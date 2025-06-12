#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import configparser
import sys
sys.stdout = open('/tmp/kiosk_debug.log', 'a', buffering=1)
sys.stderr = sys.stdout
import io
import math
import tkinter as tk
from PIL import Image, ImageTk

try:
    from libdiscid.compat import discid
except ImportError:
    import discid

import musicbrainzngs
import requests
import threading
from tkinter import messagebox
import time
import subprocess
from pydbus import SessionBus
from pydub import AudioSegment
from pydub.silence import detect_silence
from shazamio import Shazam
import time
import asyncio
import audioop

# --- GStreamer imports ---
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "kiosk_config.ini")

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

os.environ["SDL_AUDIODRIVER"] = "alsa"
ICON_DIR = os.path.join(BASE_DIR, config.get("PATHS", "ICON_DIR"))
CD_DEVICE = config.get("AUDIO", "CD_DEVICE")
ALSA_DEVICE = config.get("AUDIO", "ALSA_DEVICE")
ALBUM_ART_SIZE = (200, 200)
PCM = config.get("AUDIO", "PCM")
TMP = config.get("AUDIO", "TMP")
DURATION = config.getint("AUDIO", "DURATION")
DURATION_SHAZAM = config.getint("AUDIO", "DURATION_SHAZAM")
SILENCE_THRESHOLD = config.getint("AUDIO", "SILENCE_THRESHOLD")

# --- GStreamer Init ---
Gst.init(None)

def fetch_album_metadata():
    try:
        disc = discid.read(CD_DEVICE)
    except Exception:
        return ("Album inconnu", "Artiste inconnu", None, None)

    musicbrainzngs.set_useragent("MusicKiosk", "1.0", "contact@example.com")
    try:
        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists", "recordings"])
    except Exception:
        return ("Album inconnu", "Artiste inconnu", None, None)

    release_list = result.get("disc", {}).get("release-list", [])
    if not release_list:
        return ("Album inconnu", "Artiste inconnu", None, None)

    première_release = release_list[0]
    titre_album = première_release.get("title", "Album inconnu")
    artiste_principal = " & ".join(
        credit.get("artist", {}).get("name", "")
        for credit in première_release.get("artist-credit", [])
        if credit.get("artist", {})
    ) or "Artiste inconnu"
    mbid_release = première_release.get("id", None)
    return (titre_album, artiste_principal, mbid_release, première_release)

def fetch_cover_art(mbid_release):
    print("Mbid : ",mbid_release)
    if not mbid_release:
        return None
    try:
        url = f"https://coverartarchive.org/release/{mbid_release}/front-250"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            img = Image.open(io.BytesIO(response.content))
            return img.resize(ALBUM_ART_SIZE, Image.LANCZOS)
    except Exception:
        return None

def get_spotifyd_mpris_name(bus):
    # Accès direct au bus D-Bus pour lister les noms (org.freedesktop.DBus)
    dbus_proxy = bus.get('.DBus')
    names = dbus_proxy.ListNames()
    print("Noms DBus détectés :", names)  # Pour debug
    for name in names:
        if name.startswith("org.mpris.MediaPlayer2.spotifyd"):
            return name
    return None

class BluetoothRecognizer:
    def __init__(self, ui_callback):
        self.ui_callback = ui_callback
        self.shazam = Shazam()
        self.last_track_id = None
        self.loop_running = False

    async def recognize_song(self, filepath):
        try:
            data = await self.shazam.recognize(filepath)
            track = data.get('track', {})
            if track:
                return {
                    'title': track.get('title', 'Inconnu'),
                    'artist': track.get('subtitle', 'Inconnu'),
                    'album': track.get('sections', [{}])[0].get('metadata', [{}])[0].get('text', 'Inconnu'),
                    'cover': track.get('images', {}).get('coverart', None),
                    'id': track.get('key')
                }
        except Exception as e:
            print(f"Erreur Shazam: {e}")
        return None

    def start(self):
        if self.loop_running:
            return
        self.loop_running = True
        threading.Thread(target=self.loop, daemon=True).start()

    def stop(self):
        self.loop_running = False

    def loop(self):
        silence_detected = False

        while self.loop_running:
            self.record_audio(TMP, DURATION)
            is_silent = self.check_silence(TMP)

            if is_silent:
                print("Silence détecté.")
                time.sleep(5)
                silence_detected = True
            else:
                if self.last_track_id is None:
                    self.record_audio(TMP, DURATION_SHAZAM)
                    print("Son détecté (phase initiale), interrogation Shazam...")
                    track = asyncio.run(self.recognize_song(TMP))
                    if track and track['id'] != self.last_track_id:
                        self.last_track_id = track['id']
                        self.ui_callback(track)
                        print(f"🎶 Chanson trouvée : {track['artist']} - {track['title']}")
                elif silence_detected:
                    print("🎵 Fin de silence → son détecté → possible nouveau morceau")
                    self.record_audio(TMP, DURATION_SHAZAM)
                    track = asyncio.run(self.recognize_song(TMP))
                    if track:
                        if track['id'] != self.last_track_id:
                            self.last_track_id = track['id']
                            self.ui_callback(track)
                            print(f"🎶 Nouveau morceau détecté : {track['artist']} - {track['title']}")
                        else:
                            print("Aucune nouvelle chanson détectée.")
                    else:
                        print("Aucun titre trouvé. Réinitialisation de l’affichage.")
                        self.ui_callback({
                            'title': "Mode enceinte Bluetooth !",
                            'artist': "",
                            'album': "",
                            'cover': None
                        })
                    silence_detected = False

            time.sleep(1)

    def record_audio(self, filepath, duration):
        subprocess.run([
            "arecord", "-D", PCM, "-f", "S16_LE", "-r", "44100", "-c", "2",
            "-d", str(duration), filepath
        ], check=True)

    def check_silence(self, filepath):
        with open(filepath, "rb") as f:
            pcm_data = f.read()
        rms = audioop.rms(pcm_data, 2)
        print(f"Niveau RMS: {rms}")
        return rms < SILENCE_THRESHOLD

class MusicKioskApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Music Kiosk")
        self.attributes('-fullscreen', True)
        self.configure(background='black')

        # GStreamer pipeline & state
        self.cd_pipeline = None

        self.track_index = 1
        self.cd_playing = False

        self.last_album = ""
        self.last_artist_album = ""
        self.last_discid = None
        self.tracks_info = []

        self.volume = 40

        # -- Cadre principal horizontal
        self.main_frame = tk.Frame(self, bg='black')
        self.main_frame.pack(fill='both', expand=True)

        # -- 1. Frame du contenu à gauche (cover, infos, boutons)
        self.content_frame = tk.Frame(self.main_frame, bg='black')
        self.content_frame.pack(side='left', fill='both', expand=True, padx=30, pady=20)

        # --- Photo de l'album
        self.album_canvas = tk.Canvas(
            self.content_frame, width=ALBUM_ART_SIZE[0], height=ALBUM_ART_SIZE[1],
            bg='black', highlightthickness=0
        )
        self.album_canvas.pack(pady=(0, 15))

        # --- Titre chanson (gros)
        self.title_label = tk.Label(
            self.content_frame, text="", fg='white',
            bg='black', font=('Helvetica', 20, 'bold'), justify="center"
        )
        self.title_label.pack(pady=2)

        # --- Artiste
        self.artist_label = tk.Label(
            self.content_frame, text="", fg='white',
            bg='black', font=('Helvetica', 16), justify="center"
        )
        self.artist_label.pack(pady=2)

        # --- Titre de l'album (plus petit)
        self.album_label = tk.Label(
            self.content_frame, text="", fg='#BBB',
            bg='black', font=('Helvetica', 14, 'italic'), justify="center"
        )
        self.album_label.pack(pady=(2, 12))

        # --- Nouveau Frame pour boutons + volume
        self.controls_and_volume_frame = tk.Frame(self.content_frame, bg='black')
        self.controls_and_volume_frame.pack(pady=(8, 0), fill='x')

        # --- Contrôles musicaux (dans ce nouveau frame)
        self.ctrl_frame = tk.Frame(self.controls_and_volume_frame, bg='black')
        self.ctrl_frame.pack(side='top', pady=(0, 6), fill='x')

        self.ctrl_buttons = []
        controls = [
            ("Previous", self.prev_track),
            ("Play/Pause", self.play_pause),
            ("Next", self.next_track),
        ]
        for txt, cmd in controls:
            b = tk.Button(
                self.ctrl_frame, text=txt,
                bg='gray20', fg='white',
                padx=15, pady=10, command=cmd
            )
            b.pack(side='left', padx=20)
            self.ctrl_buttons.append(b)

        # --- Barre de volume (dans controls_and_volume_frame)
        self.volume_scale = tk.Scale(
            self.controls_and_volume_frame,
            from_=0, to=100, orient='horizontal',
            length=350,  # adapte la largeur ici (plus que 200 !)
            showvalue=True,
            resolution=1,
            sliderlength=50,
            tickinterval=25,
            bg='black',
            fg='white',
            troughcolor='#333',
            highlightthickness=0,
            command=self.on_volume_change
        )
        self.volume_scale.set(self.volume)
        self.volume_scale.pack(side='top', fill='x', padx=20)  # fill x pour la largeur

        # -- 2. Frame des icônes sources, à droite (vertical)
        self.icon_frame = tk.Frame(self.main_frame, bg='black')
        self.icon_frame.pack(side='right', fill='y', padx=30, pady=20)
        sources = [
            ("CD",        "cd.png"),
            ("Spotify",   "spotify.png"),
            ("Bluetooth", "bluetooth.png"),
        ]
        for idx, (name, fname) in enumerate(sources):
            img_path = os.path.join(ICON_DIR, fname)
            try:
                img = Image.open(img_path).resize((90, 90), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception as e:
                print(f"[WARN] Impossible de charger l'icône '{img_path}' : {e}")
                placeholder = Image.new("RGB", (90, 90), color=(50, 50, 50))
                photo = ImageTk.PhotoImage(placeholder)
            btn = tk.Button(
                self.icon_frame,
                image=photo,
                bg='black',
                bd=0,
                highlightthickness=0,
                command=lambda n=name: self.select_source(n)
            )
            btn.image = photo
            btn.grid(row=idx, column=0, pady=30)
        self.status_label = tk.Label(self.content_frame, text="Sélectionnez une source", fg='#AAA', bg='black', font=('Helvetica', 12))
        self.status_label.pack(pady=(6, 0))
        self.current_track_id = None
        self.sp = None    

    def show_cd_controls(self):
        self.controls_and_volume_frame.pack(pady=(8, 0), fill='x')
        self.ctrl_frame.pack(side='top', pady=(0, 6), fill='x')
        self.volume_scale.pack(side='top', fill='x', padx=20)

    def hide_cd_controls(self):
        self.controls_and_volume_frame.pack_forget()
    
    def select_source(self, source_name):
        # Arrête l’update Spotify si on change de source
        if hasattr(self, '_spotify_update_running'):
            self._spotify_update_running = False
        self.title_label.config(text=f"Source : {source_name}")
        self.artist_label.config(text="")
        self.album_label.config(text="")
        self.album_canvas.delete("all")
        self._stop_cd_process()
        subprocess.run(['bluetoothctl', 'power', 'off'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stop_spotifyd()
        if source_name == "CD":
            self.show_cd_controls()
            self.play_cd()
        elif source_name == "Spotify":
            self.hide_cd_controls()
            self.start_spotifyd()
            self.play_spotify()
        elif source_name == "Bluetooth":
            try:
                lsmod = subprocess.check_output(['lsmod']).decode()
                if 'snd_aloop' not in lsmod:
                    subprocess.run(['sudo', 'modprobe', 'snd-aloop'])
            except Exception as e:
                print(f"[WARN] Impossible de charger snd-aloop : {e}")
            subprocess.run(['bluetoothctl', 'power', 'on'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.hide_cd_controls()
            self.play_bluetooth()

    def play_cd(self):
        self.status_label.config(text="")
        self.title_label.config(text="Chargement du CD…")
        self.artist_label.config(text="")
        self.album_label.config(text="")
        self.album_canvas.delete("all")

        def fetch_and_display():
            print("== Début fetch_and_display ==")
            try:
                titre_album, artiste_principal, mbid, release_obj = fetch_album_metadata()
                print(f"Metadata récupérées: {titre_album}, {artiste_principal}, {mbid}")
            except Exception as e:
                print(f"[Erreur fetch_album_metadata] {e}")
                self.after(0, lambda err=e: self.title_label.config(
                    text=f"Erreur lecteur CD : {err}"))
                return

            # On prépare la tracklist et les infos
            try:
                print("== Début récupération infos disque ==")
                try:
                    disc = discid.read(CD_DEVICE)
                    num_tracks = disc.last_track_num
                    disc_id = disc.id
                    disc_number = str(getattr(disc, 'disc_number', 1))
                    print(f"Infos CD: num_tracks={num_tracks}, disc_id={disc_id}, disc_number={disc_number}")
                except Exception as e:
                    print(f"[Erreur discid.read] {e}")
                    num_tracks = 1
                    disc_id = None
                    disc_number = "1"

                medium_list = release_obj.get("medium-list", []) if release_obj else []
                print(f"medium_list: {medium_list}")
                selected_medium = None

                if disc_id:
                    for medium in medium_list:
                        disc_list = medium.get("disc-list", [])
                        if any(d.get("id") == disc_id for d in disc_list):
                            selected_medium = medium
                            break
                if not selected_medium:
                    for medium in medium_list:
                        if medium.get("position") == disc_number:
                            selected_medium = medium
                            break
                if not selected_medium and medium_list:
                    selected_medium = medium_list[0]

                print(f"selected_medium: {selected_medium}")

                # Tracklist
                tracks_info = []
                if selected_medium and selected_medium.get("track-list"):
                    for track in selected_medium["track-list"]:
                        titre_piste = track.get("recording", {}).get("title", "Titre inconnu")
                        artist_credits = track.get("artist-credit", [])
                        artiste_piste = " & ".join(
                            ac.get("artist", {}).get("name", "") for ac in artist_credits if ac.get("artist", {})
                        ) or artiste_principal
                        duree = track.get("length", None)
                        if duree:
                            minutes = int(duree) // 60000
                            secondes = (int(duree) % 60000) // 1000
                            duree_fmt = f"{minutes}:{secondes:02d}"
                        else:
                            duree_fmt = "--:--"
                        num_str = track.get("number", "").lstrip("0") or "?"
                        try:
                            num = int(num_str)
                        except ValueError:
                            num = None
                        tracks_info.append({
                            "num": num if num else len(tracks_info) + 1,
                            "titre": titre_piste,
                            "artiste": artiste_piste,
                            "duree_fmt": duree_fmt
                        })
                    # Complète si la base MB est incomplète
                    while len(tracks_info) < num_tracks:
                        tracks_info.append({
                            "num": len(tracks_info) + 1,
                            "titre": f"Piste {len(tracks_info) + 1}",
                            "artiste": artiste_principal,
                            "duree_fmt": "--:--"
                        })
                else:
                    for idx in range(1, num_tracks + 1):
                        tracks_info.append({
                            "num": idx,
                            "titre": f"Piste {idx}",
                            "artiste": artiste_principal,
                            "duree_fmt": "--:--"
                        })
                medium_title = selected_medium.get("title") if selected_medium else None
                if medium_title:
                    full_title = f"{titre_album} - {medium_title}"
                else:
                    full_title = titre_album
                print("Tracklist construite")
            except Exception as e:
                print(f"[Erreur parsing tracklist] {e}")
                self.after(0, lambda err=e: self.title_label.config(
                    text=f"Erreur lecture pistes : {err}"))
                return

            try:
                print("== Récupération de la pochette ==")
                cover = fetch_cover_art(mbid)
                print(f"Cover récupérée ? {'Oui' if cover else 'Non'}")
            except Exception as e:
                print(f"[Erreur fetch_cover_art] {e}")
                cover = None

            def update_ui():
                print("== update_ui ==")
                self.last_album = titre_album
                self.last_artist_album = artiste_principal
                self.tracks_info = tracks_info
                self.album_label.config(text=full_title)
                if cover:
                    self._display_cover(cover)
                else:
                    self.album_canvas.delete("all")
                    self.album_canvas.create_rectangle(
                        0, 0, ALBUM_ART_SIZE[0], ALBUM_ART_SIZE[1], fill="black", outline="black"
                    )
                self.track_index = 1
                self.display_track(self.track_index)
                self._start_cd_track()

            self.after(0, update_ui)

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _display_cover(self, pil_image):
        self.album_canvas.delete("all")
        self.cover_photo = ImageTk.PhotoImage(pil_image)
        self.album_canvas.create_image(
            ALBUM_ART_SIZE[0] // 2,
            ALBUM_ART_SIZE[1] // 2,
            image=self.cover_photo
        )

    def display_track(self, index):
        if 1 <= index <= len(self.tracks_info):
            track_info = self.tracks_info[index - 1]
            self.title_label.config(text=f"{track_info['titre']} ({track_info['duree_fmt']})")
            self.artist_label.config(text=track_info['artiste'])
        else:
            self.title_label.config(text=f"Piste {index}")
            self.artist_label.config(text=self.last_artist_album)

    def _start_cd_track(self):
        self._stop_cd_process()
        cdda_uri = f"cdda://{self.track_index}"
        pipeline_str = f'uridecodebin uri={cdda_uri} ! volume name=cd_volume volume={self.volume / 100.0} ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=2097152 !audioconvert ! audioresample ! alsasink device={ALSA_DEVICE}'
        print(f"Avant parse_launch : {time.time()}")
        self.cd_pipeline = Gst.parse_launch(pipeline_str)
        print(f"Après parse_launch : {time.time()}")
        buscd = self.cd_pipeline.get_bus()
        buscd.add_signal_watch()
        buscd.connect("message", self._on_gst_message)
        volume_elem = self.cd_pipeline.get_by_name("cd_volume")
        if volume_elem:
            volume_elem.set_property("volume", self.volume / 100.0)
        print(f"Avant set_state PLAYING : {time.time()}")
        self.cd_pipeline.set_state(Gst.State.PLAYING)
        print(f"Après set_state PLAYING : {time.time()}")
        self.cd_playing = True
        self.volume_scale.set(self.volume)

    def _stop_cd_process(self):
        if self.cd_pipeline:
            self.cd_pipeline.set_state(Gst.State.NULL)
            self.cd_pipeline = None
            self.cd_playing = False
    
    def start_spotifyd(self):
        # Lance spotifyd si pas déjà lancé
        if not hasattr(self, "_spotifyd_proc") or self._spotifyd_proc.poll() is not None:
            self._spotifyd_proc = subprocess.Popen(
                ["/usr/local/bin/spotifyd", "--no-daemon", "--config-path", "/home/player/.config/spotifyd/spotifyd.conf"]
            )

    def stop_spotifyd(self):
        if hasattr(self, "_spotifyd_proc") and self._spotifyd_proc.poll() is None:
            self._spotifyd_proc.terminate()

    def _on_gst_message(self, buscd, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            self.cd_pipeline.set_state(Gst.State.NULL)
            self.cd_playing = False
            if self.track_index < len(self.tracks_info):
                self.track_index += 1
                self.display_track(self.track_index)
                self._start_cd_track()
            else:
                self.title_label.config(text="Fin du CD")
        elif t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self.title_label.config(text=f"Erreur GStreamer : {err}")
            self.cd_pipeline.set_state(Gst.State.NULL)
            self.cd_playing = False

    def _pause_cd(self):
        if self.cd_pipeline:
            self.cd_pipeline.set_state(Gst.State.PAUSED)
            self.cd_playing = False
            self.title_label.config(text=f"(Pause) {self.title_label['text']}")

    def _resume_cd(self):
        if self.cd_pipeline:
            self.cd_pipeline.set_state(Gst.State.PLAYING)
            self.cd_playing = True

    def play_pause(self):
        if self.cd_playing:
            self._pause_cd()
        else:
            self._resume_cd()
        self.display_track(self.track_index)

    def prev_track(self):
        if self.track_index > 1:
            self.track_index -= 1
            self.display_track(self.track_index)
            self._start_cd_track()

    def next_track(self):
        if self.track_index < len(self.tracks_info):
            self.track_index += 1
            self.display_track(self.track_index)
            self._start_cd_track()
    
    def on_volume_change(self, val):
        try:
            self.volume = int(val)
            if self.cd_pipeline:
                volume_elem = self.cd_pipeline.get_by_name("cd_volume")
                if volume_elem:
                    volume_elem.set_property("volume", self.volume / 100.0)
        except Exception as e:
            print(f"[Volume] Erreur lors du changement de volume : {e}")

    def play_spotify(self):
        self.status_label.config(text="Lecture Spotify Connect")
        self.title_label.config(text="Lecture Spotify…")
        self.artist_label.config(text="")
        self.album_label.config(text="")
        self.album_canvas.delete("all")
        self._stop_cd_process()

        # Variable pour éviter le multi-thread d’update
        self._spotify_update_running = True

        def update_spotify_info():
            last_track_id = None

            while getattr(self, '_spotify_update_running', False):
                try:
                    bus = SessionBus()
                    service_name = get_spotifyd_mpris_name(bus)
                    if not service_name:
                        raise Exception("Kiosk")
                    player = bus.get(service_name, "/org/mpris/MediaPlayer2")
                    metadata = player.Metadata
                    title = metadata.get('xesam:title', 'Inconnu')
                    artists = ', '.join(metadata.get('xesam:artist', []))
                    album = metadata.get('xesam:album', '')
                    track_obj = metadata.get('mpris:trackid', None)
                    track_uri = str(track_obj) if track_obj is not None else None
                    if track_uri != last_track_id:
                        # Utilise directement la cover URL fournie (plus fiable et instantané)
                        cover_url = metadata.get('mpris:artUrl', None)
                        if cover_url:
                            try:
                                response = requests.get(cover_url, timeout=4)
                                img_data = response.content
                                img = Image.open(io.BytesIO(img_data)).resize(ALBUM_ART_SIZE, Image.LANCZOS)
                                self.after(0, lambda img=img: self._display_cover(img))
                            except Exception:
                                self.after(0, lambda: self.album_canvas.delete("all"))
                        else:
                            self.after(0, lambda: self.album_canvas.delete("all"))

                        self.after(0, lambda: self.title_label.config(text=title))
                        self.after(0, lambda: self.artist_label.config(text=artists))
                        self.after(0, lambda: self.album_label.config(text=album))
                        last_track_id = track_uri

                except Exception as e:
                    self.after(0, lambda err=e: self.title_label.config(text=f"Attente Connexion : {err}"))
                    self.after(0, lambda: self.artist_label.config(text=""))
                    self.after(0, lambda: self.album_label.config(text=""))
                    self.after(0, lambda: self.album_canvas.delete("all"))


                time.sleep(1)

        # Lance la mise à jour dans un thread dédié
        threading.Thread(target=update_spotify_info, daemon=True).start()

    # ... dans ta classe MusicKioskApp, dans play_bluetooth() :
    def play_bluetooth(self):
        self.title_label.config(text="Mode enceinte Bluetooth !")
        self.artist_label.config(text="")
        self.album_label.config(text="")
        self.album_canvas.delete("all")

        if hasattr(self, 'bluetooth_recognizer'):
            self.bluetooth_recognizer.stop()

        self.bluetooth_recognizer = BluetoothRecognizer(self.update_ui_from_shazam)
        self.bluetooth_recognizer.start()

    def update_ui_from_shazam(self, track_info):
        def ui_update():
            self.title_label.config(text=track_info['title'])
            self.artist_label.config(text=track_info['artist'])
            self.album_label.config(text=track_info['album'])

            if track_info['cover']:
                try:
                    response = requests.get(track_info['cover'], timeout=5)
                    img = Image.open(io.BytesIO(response.content)).resize(ALBUM_ART_SIZE, Image.LANCZOS)
                    self._display_cover(img)
                except:
                    self.album_canvas.delete("all")
            else:
                self.album_canvas.delete("all")
        self.after(0, ui_update)

if __name__ == '__main__':
    # Boucle Tkinter ET GStreamer mainloop (intégration)
    import threading

    def run_glib():
        loop = GLib.MainLoop()
        loop.run()

    # Lance GLib mainloop dans un thread pour GStreamer (bus, EOS, etc.)
    glib_thread = threading.Thread(target=run_glib, daemon=True)
    glib_thread.start()

    # Lance l'interface
    app = MusicKioskApp()
    app.mainloop()
