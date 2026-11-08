import os
import sqlite3
import hashlib
import random
import string
import argparse
from pathlib import Path
import requests

AUDIO_EXTENSIONS = {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.aac', '.wma', '.alac'}

def get_subsonic_auth(username, password):
    """Genera los parámetros de autenticación requeridos por la API de Subsonic/Navidrome."""
    salt = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    token = hashlib.md5((password + salt).encode('utf-8')).hexdigest()
    return {
        'u': username,
        't': token,
        's': salt,
        'v': '1.16.1',
        'c': 'NavidromeM3U8Sync', #acceder a navidrome para habilitar ReportRealPath para este cliente
        'f': 'json'
    }

def setup_database(db_path, device_path, force_rebuild=False):
    """Crea o conecta a la base de datos SQLite e indexa los archivos de audio."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audio_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            relative_path TEXT NOT NULL
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_filename ON audio_files(filename)')
    conn.commit()

    
    cursor.execute('SELECT COUNT(*) FROM audio_files')
    count = cursor.fetchone()[0]

    if count > 0 and not force_rebuild:
        print(f"[Info] Usando la base de datos existente.")
        return conn

    if force_rebuild or count == 0:
        print(f"[Info] Escaneando la unidad '{device_path}' para indexar archivos de audio...")
        cursor.execute('DELETE FROM audio_files')
        conn.commit()

        device_root = Path(device_path)
        if not device_root.exists():
            raise FileNotFoundError(f"La ruta '{device_path}' no existe.")

        indexed_count = 0
        for file_path in device_root.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:

                try:
                    rel_to_device = file_path.relative_to(device_root)
                    formatted_path = '/' + str(rel_to_device).replace('\\', '/')
                    filename = file_path.name

                    cursor.execute(
                        'INSERT INTO audio_files (filename, relative_path) VALUES (?, ?)',
                        (filename, formatted_path)
                    )
                    indexed_count += 1
                except Exception as e:
                    print(f"[Advertencia] No se pudo procesar el archivo {file_path}: {e}")

        conn.commit()
        print(f"[Info] Indexación completa, {indexed_count} archivos de audio.")

    return conn

def fetch_navidrome_playlists(server_url, auth_params):
    """Obtiene la lista de playlists desde Navidrome."""
    url = f"{server_url.rstrip('/')}/rest/getPlaylists.view"
    try:
        response = requests.get(url, params=auth_params)
        response.raise_for_status()
        data = response.json()
        
        subsonic_response = data.get('subsonic-response', {})
        if subsonic_response.get('status') == 'failed':
            error_msg = subsonic_response.get('error', {}).get('message', 'Error desconocido')
            raise Exception(f"Error en la API de Navidrome: {error_msg}")
            
        playlists = subsonic_response.get('playlists', {}).get('playlist', [])
        return playlists if isinstance(playlists, list) else [playlists] if playlists else []
    except requests.exceptions.RequestException as e:
        raise Exception(f"No se pudo conectar al servidor Navidrome: {e}")

def fetch_playlist_tracks(server_url, auth_params, playlist_id):
    """Obtiene los detalles y tracks de una playlist específica."""
    url = f"{server_url.rstrip('/')}/rest/getPlaylist.view"
    params = auth_params.copy()
    params['id'] = playlist_id
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    
    subsonic_response = data.get('subsonic-response', {})
    playlist_data = subsonic_response.get('playlist', {})
    tracks = playlist_data.get('entry', [])
    
    return tracks if isinstance(tracks, list) else [tracks] if tracks else []

def main():
    parser = argparse.ArgumentParser(description="Sincronizador de Playlists de Navidrome a dispositivo local.")
    parser.add_argument('--url', required=True, help="URL del servidor Navidrome (ej. http://localhost:4533)")
    parser.add_argument('--user', required=True, help="Nombre de usuario de Navidrome")
    parser.add_argument('--password', required=True, help="Contraseña de Navidrome")
    parser.add_argument('--device', required=True, help="Ruta de la unidad de almacenamiento (ej. G:\\ o /media/usb)")

    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    db_path = script_dir / 'audio_index.db'

    force_rebuild = False
    if db_path.exists():
        choice = input(f"La base de datos ya existe en '{db_path}'. ¿Desea re-generarla? (s/n): ").strip().lower()
        if choice == 's':
            force_rebuild = True

    try:
        db_conn = setup_database(db_path, args.device, force_rebuild)
    except Exception as e:
        print(f"[Error] Fallo al configurar la base de datos: {e}")
        return

    cursor = db_conn.cursor()


    print("[Info] Conectando a Navidrome...")
    try:
        auth = get_subsonic_auth(args.user, args.password)
        playlists = fetch_navidrome_playlists(args.url, auth)
        print(f"[Info] Se encontraron {len(playlists)} playlists en Navidrome.")
    except Exception as e:
        print(f"[Error] {e}")
        db_conn.close()
        return

    device_playlist_dir = Path(args.device) / 'Playlists'
    device_playlist_dir.mkdir(parents=True, exist_ok=True)

    for pl in playlists:
        pl_id = pl.get('id')
        pl_name = pl.get('name')
        
        safe_pl_name = "".join(c for c in pl_name if c.isalnum() or c.rstrip() in (' ', '-', '_')).strip()
        if not safe_pl_name:
            safe_pl_name = f"playlist_{pl_id}"

        m3u8_file_path = device_playlist_dir / f"{safe_pl_name}.m3u8"

        if m3u8_file_path.exists():
            choice = input(f"La playlist '{pl_name}.m3u8' ya existe en el dispositivo. ¿Desea reemplazarla? (s/n): ").strip().lower()
            if choice != 's':
                print(f"[Info] Omitiendo playlist: {pl_name}")
                continue

        try:
            tracks = fetch_playlist_tracks(args.url, auth, pl_id)
        except Exception as e:
            print(f"[Advertencia] No se pudieron obtener los tracks de la playlist '{pl_name}': {e}")
            continue

        matched_paths = []
        for track in tracks:
            track_path_server = track.get('path', '') or track.get('title', '')
            filename = track_path_server.split('/').pop()
            
            if not filename:
                continue

            cursor.execute('SELECT relative_path FROM audio_files WHERE filename = ?', (filename,))
            row = cursor.fetchone()

            if row:
                matched_paths.append(row[0])
            else:
                # nada por hacer, continua si no está el archivo en el dispositivo
                pass

        try:
            with open(m3u8_file_path, 'w', encoding='utf-8') as f:
                f.write("#EXTM3U\n")
                for path in matched_paths:
                    f.write(f"{path}\n")
            print(f"[Éxito] Playlist generada: {m3u8_file_path.name} ({len(matched_paths)} tracks añadidos)")
        except Exception as e:
            print(f"[Error] No se pudo escribir el archivo m3u8 para '{pl_name}': {e}")

    db_conn.close()
    print("[Info] Proceso de sincronización finalizado.")

if __name__ == '__main__':
    main()