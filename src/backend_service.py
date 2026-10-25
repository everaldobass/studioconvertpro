# backend_service.py
#
# Lógica do Studio Convert Pro (sem interface gráfica):
# download do YouTube em MP3, análise de BPM/tom, listagem das músicas
# locais, separação de faixas (Demucs) e o reprodutor de áudio.

import collections
import json
import os
import re
import subprocess
import sys
import threading

from gi.repository import GLib


def _pasta_musicas():
    """Pasta de músicas do usuário (Músicas, Music...), conforme o idioma do sistema."""
    pasta = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
    return pasta or os.path.expanduser("~/Music")


# Garante que os diretórios locais existam
BASE_DIR = os.path.join(_pasta_musicas(), "StudioConvert")
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
STEMS_DIR = os.path.join(BASE_DIR, "stems")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(STEMS_DIR, exist_ok=True)

# Cópias temporárias em WAV usadas só pelo reprodutor (pode apagar a qualquer hora)
PLAYER_CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "studioconvertpro", "player")

# Faixas que o Demucs (modelo htdemucs) separa
STEMS = ("vocals", "drums", "bass", "other")
MODELO_STEMS = "htdemucs"


def carregar_musicas_locais():
    """Retorna a lista de arquivos MP3 baixados, sem a extensão .mp3."""
    if not os.path.exists(DOWNLOADS_DIR):
        return []
    musicas = [
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOADS_DIR)
        if f.lower().endswith(".mp3")
    ]
    return sorted(musicas)


def caminho_da_musica(nome_musica):
    """Caminho completo do MP3 a partir do nome exibido na playlist."""
    return os.path.join(DOWNLOADS_DIR, nome_musica + ".mp3")


def analisar_bpm_e_tom(caminho_mp3):
    """Analisa o BPM e a tonalidade musical usando librosa."""
    try:
        # Importação tardia: librosa é pesada e atrasaria a abertura do app
        import librosa
        import numpy as np

        y, sr = librosa.load(caminho_mp3, duration=30)  # só os primeiros 30s
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)

        # Versões novas do librosa devolvem um array numpy
        tempo = float(np.atleast_1d(tempo)[0])
        bpm_estimado = str(int(round(tempo)))

        chroma = librosa.feature.chroma_stft(y=y, sr=sr)
        tonalidades = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        tom_estimado = tonalidades[int(np.argmax(np.sum(chroma, axis=1)))]

        return bpm_estimado, tom_estimado
    except Exception as e:
        print(f"Erro ao analisar audio: {e}")
        return "---", "---"


ANALISE_CACHE = os.path.join(GLib.get_user_cache_dir(), "studioconvertpro", "analise.json")
_trava_analise = threading.Lock()


def analisar_com_cache(caminho_mp3):
    """Como analisar_bpm_e_tom, mas lembra o resultado de cada MP3.

    O cache fica em ~/.cache (pode apagar); se o MP3 mudar, é recalculado.
    Falhas ("---") não são guardadas, então uma nova tentativa é feita depois.
    Pode demorar alguns segundos: chame fora da thread da interface.
    """
    with _trava_analise:
        try:
            chave = os.path.basename(caminho_mp3)
            carimbo = int(os.path.getmtime(caminho_mp3))
        except OSError:
            return "---", "---"

        cache = {}
        try:
            with open(ANALISE_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            pass

        item = cache.get(chave)
        if isinstance(item, dict) and item.get("mtime") == carimbo:
            return item.get("bpm", "---"), item.get("tom", "---")

        bpm, tom = analisar_bpm_e_tom(caminho_mp3)
        if bpm != "---" and tom != "---":
            cache[chave] = {"mtime": carimbo, "bpm": bpm, "tom": tom}
            try:
                os.makedirs(os.path.dirname(ANALISE_CACHE), exist_ok=True)
                with open(ANALISE_CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            except OSError:
                pass
        return bpm, tom


def baixar_audio_youtube(url):
    """Baixa e converte um link do YouTube para MP3.

    Retorna (sucesso, mensagem, caminho_do_arquivo).
    """
    if not url:
        return False, "URL inválida.", None

    try:
        import yt_dlp
    except ImportError:
        return False, "yt-dlp não está instalado (pip install yt-dlp).", None

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            titulo = info.get('title', 'Audio Baixado')
            # prepare_filename devolve o nome real (o yt-dlp troca "/", ":" etc.)
            caminho_final = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
            return True, f"Sucesso: '{titulo}' baixado!", caminho_final
    except Exception as e:
        return False, f"Erro no download: {str(e)}", None


# ----------------------------------------------------------------------
# Preparo de áudio para o reprodutor
# ----------------------------------------------------------------------
def preparar_audio_para_player(caminho_mp3):
    """Converte o MP3 em WAV (com o ffmpeg do app) para o reprodutor tocar.

    Devolve (caminho_wav, None) ou (None, mensagem_de_erro).
    Pode demorar um ou dois segundos: chame fora da thread da interface.
    """
    try:
        os.makedirs(PLAYER_CACHE_DIR, exist_ok=True)
        nome = os.path.splitext(os.path.basename(caminho_mp3))[0]
        wav = os.path.join(PLAYER_CACHE_DIR, nome + ".wav")

        # Reaproveita a conversão anterior se o MP3 não mudou
        if os.path.exists(wav) and os.path.getmtime(wav) >= os.path.getmtime(caminho_mp3):
            return wav, None

        temporario = wav + ".part"
        comando = [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-i", caminho_mp3,
            "-vn", "-ac", "2", "-ar", "44100",
            "-c:a", "pcm_s16le", "-f", "wav", temporario,
        ]
        resultado = subprocess.run(comando, capture_output=True, text=True, timeout=300)
        if resultado.returncode != 0:
            return None, resultado.stderr.strip() or "o ffmpeg falhou ao converter o áudio."
        os.replace(temporario, wav)
        return wav, None
    except FileNotFoundError:
        return None, "ffmpeg não encontrado."
    except Exception as e:
        return None, str(e)


# ----------------------------------------------------------------------
# Separação de faixas (Demucs)
# ----------------------------------------------------------------------
def stems_da_musica(nome_musica):
    """Caminhos esperados das 4 faixas separadas: {'vocals': '.../vocals.wav', ...}."""
    pasta = os.path.join(STEMS_DIR, nome_musica, MODELO_STEMS)
    return {stem: os.path.join(pasta, f"{stem}.wav") for stem in STEMS}


def stems_prontos(nome_musica):
    """True se as 4 faixas já foram separadas para esta música."""
    return all(
        os.path.exists(caminho) and os.path.getsize(caminho) > 0
        for caminho in stems_da_musica(nome_musica).values()
    )


class ControleSeparacao:
    """Permite cancelar uma separação em andamento (ex.: ao fechar o app)."""

    def __init__(self):
        self.processo = None
        self.cancelado = False

    def cancelar(self):
        self.cancelado = True
        if self.processo is not None and self.processo.poll() is None:
            self.processo.terminate()


def separar_faixas(caminho_mp3, nome_musica, ao_progresso=None, controle=None):
    """Separa a música em vocals/drums/bass/other com o Demucs.

    O Demucs roda em um processo separado (uma falha dele não derruba o app).
    ao_progresso(fase: str, percentual: int | None) é chamado durante o trabalho,
    sempre da thread de trabalho: use GLib.idle_add para mexer na interface.

    Devolve (sucesso, mensagem). Pode levar vários minutos em CPU: chame fora
    da thread da interface.
    """
    controle = controle or ControleSeparacao()

    def avisar(fase, percentual=None):
        if ao_progresso:
            ao_progresso(fase, percentual)

    avisar("Preparando áudio", None)
    wav, erro = preparar_audio_para_player(caminho_mp3)
    if erro:
        return False, f"Erro ao preparar o áudio: {erro}"
    if controle.cancelado:
        return False, "Separação cancelada."

    comando = [
        sys.executable, "-m", "demucs.separate",
        "-n", MODELO_STEMS,
        "-d", "cpu",
        "-o", os.path.join(STEMS_DIR, nome_musica),
        "--filename", "{stem}.{ext}",
        wav,
    ]
    ambiente = dict(os.environ, PYTHONUNBUFFERED="1")

    try:
        processo = subprocess.Popen(
            comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ambiente
        )
    except Exception as e:
        return False, f"Não foi possível iniciar o Demucs: {e}"
    controle.processo = processo

    avisar("Iniciando IA", None)
    ultimas_linhas = collections.deque(maxlen=12)
    pendente = ""
    while True:
        pedaco = processo.stdout.read1(512)
        if not pedaco:
            break
        pendente += pedaco.decode("utf-8", errors="replace")
        # A barra de progresso (tqdm) usa "\r" para reescrever a mesma linha
        linhas = re.split(r"[\r\n]", pendente)
        pendente = linhas.pop()
        for linha in linhas:
            linha = linha.strip()
            if not linha:
                continue
            ultimas_linhas.append(linha)
            achou = re.search(r"(\d{1,3})%\|", linha)
            if achou:
                baixando_modelo = any(p in linha for p in (".safetensors", ".th", "Download"))
                avisar("Baixando modelo de IA" if baixando_modelo else "Separando faixas",
                       min(100, int(achou.group(1))))
    processo.wait()

    if controle.cancelado:
        return False, "Separação cancelada."
    if processo.returncode != 0:
        detalhe = " | ".join(list(ultimas_linhas)[-3:]) or f"código {processo.returncode}"
        return False, f"O Demucs falhou: {detalhe}"
    if not stems_prontos(nome_musica):
        return False, "O Demucs terminou, mas as faixas não foram encontradas."
    return True, "Faixas separadas! Use os controles de volume."


# ----------------------------------------------------------------------
# Reprodutor de áudio
# ----------------------------------------------------------------------
class Reprodutor:
    """Reprodutor simples sobre GStreamer, com pipeline montado à mão.

    Usa só elementos básicos (filesrc ! wavparse ! ... ! autoaudiosink) e NÃO usa
    playbin/decodebin3, que derrubam o app com a falha
    "mq_slot_handle_stream_start: assertion failed: (collection)".

    Dois modos:
      - carregar(wav): toca um único arquivo.
      - carregar_stems({...}): toca as 4 faixas juntas em um mixer, cada uma
        com volume próprio (set_volume_stem).

    Os callbacks são chamados na thread da interface:
      ao_mudar_estado(tocando: bool), ao_terminar(), ao_erro(mensagem: str)
    """

    def __init__(self, ao_mudar_estado=None, ao_terminar=None, ao_erro=None):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._Gst = Gst
        self._ao_mudar_estado = ao_mudar_estado
        self._ao_terminar = ao_terminar
        self._ao_erro = ao_erro

        self._pipeline = None
        self._bus = None
        self._bus_ids = []
        self._volume_el = None
        self._volume = 0.8
        self._vol_stem_els = {}
        self._vol_stems = {stem: 1.0 for stem in STEMS}
        self._modo_stems = False
        self._tocando = False

    @property
    def carregado(self):
        return self._pipeline is not None

    @property
    def tocando(self):
        return self._tocando

    @property
    def modo_stems(self):
        return self._modo_stems

    def carregar(self, caminho_wav):
        """Prepara um único arquivo WAV (sem tocar). Devolve (ok, mensagem_de_erro)."""
        self.parar()
        try:
            pipeline = self._Gst.parse_launch(
                "filesrc name=src ! wavparse ! audioconvert ! audioresample "
                "! volume name=vol ! autoaudiosink"
            )
        except GLib.Error as e:
            return False, f"Não foi possível montar o player: {e.message}"

        pipeline.get_by_name("src").set_property("location", caminho_wav)
        self._instalar(pipeline, modo_stems=False)
        return True, None

    def carregar_stems(self, caminhos):
        """Prepara as 4 faixas (dict stem -> caminho do WAV) no mixer.

        Devolve (ok, mensagem_de_erro).
        """
        self.parar()
        descricao = (
            "audiomixer name=mix ! audioconvert ! audioresample "
            "! volume name=vol ! autoaudiosink "
        ) + " ".join(
            f"filesrc name=src_{stem} ! wavparse ! audioconvert ! audioresample "
            f"! volume name=vol_{stem} ! mix."
            for stem in STEMS
        )
        try:
            pipeline = self._Gst.parse_launch(descricao)
        except GLib.Error as e:
            return False, f"Não foi possível montar o mixer: {e.message}"

        for stem in STEMS:
            pipeline.get_by_name(f"src_{stem}").set_property("location", caminhos[stem])
        self._instalar(pipeline, modo_stems=True)
        return True, None

    def tocar(self):
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.PLAYING)
            self._definir_estado(True)

    def pausar(self):
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.PAUSED)
            self._definir_estado(False)

    def parar(self):
        """Para e descarta o que estiver carregado."""
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            if self._bus is not None:
                for handler_id in self._bus_ids:
                    self._bus.disconnect(handler_id)
                self._bus.remove_signal_watch()
            self._pipeline = None
            self._bus = None
            self._bus_ids = []
            self._volume_el = None
            self._vol_stem_els = {}
            self._modo_stems = False
        self._definir_estado(False)

    def set_volume(self, volume):
        """Volume geral, de 0.0 a 1.0."""
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._volume_el is not None:
            self._volume_el.set_property("volume", self._volume)

    def set_volume_stem(self, stem, volume):
        """Volume de uma faixa (vocals, drums, bass, other), de 0.0 a 1.0."""
        self._vol_stems[stem] = max(0.0, min(1.0, float(volume)))
        elemento = self._vol_stem_els.get(stem)
        if elemento is not None:
            elemento.set_property("volume", self._vol_stems[stem])

    # --- internos -----------------------------------------------------
    def _instalar(self, pipeline, modo_stems):
        self._volume_el = pipeline.get_by_name("vol")
        self._volume_el.set_property("volume", self._volume)
        if modo_stems:
            self._vol_stem_els = {s: pipeline.get_by_name(f"vol_{s}") for s in STEMS}
            for stem, elemento in self._vol_stem_els.items():
                elemento.set_property("volume", self._vol_stems[stem])

        self._bus = pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_ids = [
            self._bus.connect("message::eos", self._on_eos),
            self._bus.connect("message::error", self._on_error),
        ]
        self._pipeline = pipeline
        self._modo_stems = modo_stems

    def _definir_estado(self, tocando):
        if tocando != self._tocando:
            self._tocando = tocando
            if self._ao_mudar_estado:
                self._ao_mudar_estado(tocando)

    def _on_eos(self, bus, message):
        # Fim da música: volta ao início (um novo Play toca de novo)
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
        self._definir_estado(False)
        if self._ao_terminar:
            self._ao_terminar()

    def _on_error(self, bus, message):
        erro, _detalhe = message.parse_error()
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
        self._definir_estado(False)
        if self._ao_erro:
            self._ao_erro(erro.message)
