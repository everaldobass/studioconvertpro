# window.py
#
# Copyright 2026 Everaldo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import threading

from gi.repository import Gtk, Adw, GLib
from .backend_service import (
    baixar_audio_youtube,
    analisar_com_cache,
    carregar_musicas_locais,
    caminho_da_musica,
    preparar_audio_para_player,
    stems_da_musica,
    stems_prontos,
    separar_faixas,
    ControleSeparacao,
    Reprodutor,
)

# Cada slider da mesa de mixagem controla uma faixa separada pelo Demucs
SLIDERS_STEMS = (
    ("vocals", "slider_vocal"),
    ("drums", "slider_bateria"),
    ("bass", "slider_baixo"),
    ("other", "slider_outro"),
)


@Gtk.Template(resource_path='/com/github/everaldo/studioconvertpro/window.ui')
class StudioConvertWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'StudioConvertWindow'

    # Coluna esquerda
    url_entry = Gtk.Template.Child()
    btn_limpar = Gtk.Template.Child()
    btn_converter = Gtk.Template.Child()
    status_box = Gtk.Template.Child()
    playlist_listbox = Gtk.Template.Child()
    btn_excluir = Gtk.Template.Child()

    # Coluna direita (stems e metadados)
    bpm_box = Gtk.Template.Child()
    key_box = Gtk.Template.Child()
    btn_processar = Gtk.Template.Child()
    status_processamento = Gtk.Template.Child()

    slider_vocal = Gtk.Template.Child()
    slider_bateria = Gtk.Template.Child()
    slider_baixo = Gtk.Template.Child()
    slider_outro = Gtk.Template.Child()

    # Reprodutor no rodapé
    lbl_tocando_agora = Gtk.Template.Child()
    btn_player_anterior = Gtk.Template.Child()
    btn_player_play = Gtk.Template.Child()
    btn_player_proximo = Gtk.Template.Child()
    volume_slider = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Estado do reprodutor
        self.musica_atual = None          # nome (sem .mp3) da música carregada
        self._carga_id = 0                # identifica o preparo de áudio mais recente
        self._carregando = False          # True enquanto o MP3 está sendo preparado
        self._tocar_quando_pronto = False
        self.player = None
        self._erro_player = None
        try:
            self.player = Reprodutor(
                ao_mudar_estado=self.on_player_estado,
                ao_terminar=self.on_player_terminou,
                ao_erro=self.on_player_erro,
            )
            self.player.set_volume(self.volume_slider.get_value() / 100.0)
        except Exception as e:
            self._erro_player = str(e)

        # Estado da análise de BPM/tom
        self._analise_id = 0
        self._analise_resultado = None    # (nome, bpm, tom) da última análise concluída

        # Estado da separação de faixas
        self._separando = False
        self._controle_separacao = None

        # Eventos da coluna esquerda
        self.btn_limpar.connect('clicked', self.on_btn_limpar_clicked)
        self.btn_converter.connect('clicked', self.on_btn_converter_clicked)
        self.btn_excluir.connect('clicked', self.on_btn_excluir_clicked)

        # Separação de faixas e mesa de mixagem
        self.btn_processar.connect('clicked', self.on_btn_processar_clicked)
        for stem, nome_slider in SLIDERS_STEMS:
            getattr(self, nome_slider).connect('value-changed', self.on_slider_stem_changed, stem)
        self._definir_mixer_ativo(False)

        # Eventos do reprodutor
        self.btn_player_play.connect('clicked', self.on_play_clicked)
        self.btn_player_anterior.connect('clicked', lambda b: self._tocar_vizinha(-1))
        self.btn_player_proximo.connect('clicked', lambda b: self._tocar_vizinha(1))
        self.volume_slider.connect('value-changed', self.on_volume_changed)
        self.playlist_listbox.connect('row-selected', self.on_playlist_row_selected)
        # Duplo clique (ou Enter) numa música: toca na hora
        self.playlist_listbox.connect('row-activated', self.on_playlist_row_activated)

        # Ao fechar a janela, encerra a separação e o áudio
        self.connect('close-request', self.on_close_request)

        # Carrega a playlist
        self.atualizar_lista_playlist()

    def on_close_request(self, window):
        if self._controle_separacao is not None:
            self._controle_separacao.cancelar()
        if self.player is not None:
            self.player.parar()
        return False  # deixa a janela fechar

    # ------------------------------------------------------------------
    # Conversor
    # ------------------------------------------------------------------
    def on_btn_limpar_clicked(self, button):
        self.url_entry.set_text("")
        self.status_box.set_text("Pronto")

    def on_btn_converter_clicked(self, button):
        url = self.url_entry.get_text().strip()
        if not url:
            self.status_box.set_text("Cole um link do YouTube antes de prosseguir.")
            return

        # O download roda em segundo plano para a janela não travar
        self.btn_converter.set_sensitive(False)
        self.status_box.set_text("Baixando áudio... Por favor aguarde.")
        threading.Thread(target=self._baixar_em_segundo_plano, args=(url,), daemon=True).start()

    def _baixar_em_segundo_plano(self, url):
        """Roda fora da thread da interface: NÃO mexa nos widgets aqui."""
        sucesso, mensagem, caminho_arquivo = baixar_audio_youtube(url)
        if sucesso and caminho_arquivo:
            # Já deixa BPM e tom no cache: ao selecionar a música aparecem na hora
            analisar_com_cache(caminho_arquivo)
        # Devolve o resultado para a thread da interface
        GLib.idle_add(self._download_concluido, sucesso, mensagem)

    def _download_concluido(self, sucesso, mensagem):
        self.btn_converter.set_sensitive(True)
        self.status_box.set_text(mensagem)
        if sucesso:
            self.atualizar_lista_playlist()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # BPM e tom da música selecionada
    # ------------------------------------------------------------------
    def _mostrar_bpm_tom(self, bpm, tom):
        self.bpm_box.set_text(f"BPM: {bpm}")
        self.key_box.set_text(f"Tom: {tom}")

    def _iniciar_analise(self, nome_musica):
        """Calcula BPM e tom em segundo plano (resultado fica no cache)."""
        self._analise_id += 1
        caminho = caminho_da_musica(nome_musica)

        # Já calculado para esta música: não pisca "analisando"
        if self._analise_resultado and self._analise_resultado[0] == nome_musica:
            self._mostrar_bpm_tom(*self._analise_resultado[1:])
            return

        self._mostrar_bpm_tom("analisando...", "analisando...")
        threading.Thread(
            target=self._analisar_em_segundo_plano,
            args=(self._analise_id, nome_musica, caminho),
            daemon=True,
        ).start()

    def _analisar_em_segundo_plano(self, analise_id, nome_musica, caminho):
        """Roda fora da thread da interface: NÃO mexa nos widgets aqui."""
        bpm, tom = analisar_com_cache(caminho)
        GLib.idle_add(self._analise_concluida, analise_id, nome_musica, bpm, tom)

    def _analise_concluida(self, analise_id, nome_musica, bpm, tom):
        # O usuário já escolheu outra música enquanto esta era analisada
        if analise_id != self._analise_id:
            return GLib.SOURCE_REMOVE
        if bpm != "---":
            self._analise_resultado = (nome_musica, bpm, tom)
        self._mostrar_bpm_tom(bpm, tom)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Playlist
    # ------------------------------------------------------------------
    def on_playlist_row_selected(self, listbox, row):
        if row is None:
            return
        nome_musica = row.get_child().get_text()
        # Já é a música carregada (ou sendo preparada): não recarrega
        if nome_musica == self.musica_atual and (
                self._carregando or (self.player is not None and self.player.carregado)):
            return
        self._carregar_musica(nome_musica, tocar=False)

    def on_playlist_row_activated(self, listbox, row):
        nome_musica = row.get_child().get_text()
        if nome_musica == self.musica_atual:
            if self.player is not None and self.player.carregado:
                self.player.tocar()
                return
            if self._carregando:
                self._tocar_quando_pronto = True
                return
        self._carregar_musica(nome_musica, tocar=True)

    def on_btn_excluir_clicked(self, button):
        row = self.playlist_listbox.get_selected_row()
        if row:
            nome = row.get_child().get_text()
            if nome == self.musica_atual:
                self._descarregar_media()
                self.musica_atual = None
                self._analise_id += 1          # descarta análise em andamento
                self._analise_resultado = None
                self.bpm_box.set_text("")      # volta ao texto de exemplo (BPM: ---)
                self.key_box.set_text("")
                self.lbl_tocando_agora.set_text("Nenhuma música selecionada")
            self.playlist_listbox.remove(row)
            self.status_box.set_text(f"Excluido: {nome}")
        else:
            self.status_box.set_text("Selecione um item na lista para excluir.")

    def atualizar_lista_playlist(self):
        # Esvazia a lista atual
        while True:
            row = self.playlist_listbox.get_row_at_index(0)
            if row is None:
                break
            self.playlist_listbox.remove(row)

        # Recria a partir dos MP3 da pasta de downloads
        musicas = carregar_musicas_locais()
        for nome_musica in musicas:
            label = Gtk.Label(label=nome_musica)
            label.set_xalign(0)
            label.set_margin_start(8)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            self.playlist_listbox.append(label)

        # Mantém marcada a música que está tocando
        if self.musica_atual in musicas:
            row = self.playlist_listbox.get_row_at_index(musicas.index(self.musica_atual))
            if row is not None:
                self.playlist_listbox.select_row(row)

    # ------------------------------------------------------------------
    # Reprodutor
    # ------------------------------------------------------------------
    def _descarregar_media(self):
        """Para a música atual e cancela qualquer preparo em andamento."""
        self._carga_id += 1
        self._carregando = False
        self._tocar_quando_pronto = False
        if self.player is not None:
            self.player.parar()
        self._definir_mixer_ativo(False)
        self.btn_player_play.set_label("▶ Play")

    def _carregar_musica(self, nome_musica, tocar=False):
        """Carrega a música no player.

        Se as faixas já foram separadas, usa o mixer de 4 faixas. Senão, converte
        o MP3 em WAV em segundo plano e toca a mixagem original.
        """
        self._descarregar_media()

        if self.player is None:
            self.status_box.set_text(f"Player indisponível: {self._erro_player}")
            return

        caminho = caminho_da_musica(nome_musica)
        if not os.path.exists(caminho):
            self.status_box.set_text(f"Arquivo não encontrado: {nome_musica}.mp3")
            return

        self.musica_atual = nome_musica
        self.lbl_tocando_agora.set_text(nome_musica)
        self._iniciar_analise(nome_musica)

        # Faixas já separadas: carrega direto no mixer
        if stems_prontos(nome_musica):
            ok, mensagem = self.player.carregar_stems(stems_da_musica(nome_musica))
            if ok:
                self._reiniciar_sliders_stems()
                self._definir_mixer_ativo(True)
                self.status_box.set_text(f"Selecionada: {nome_musica} (mixer de faixas ativo)")
                if tocar:
                    self.player.tocar()
                return
            self.status_box.set_text(f"Mixer indisponível ({mensagem}). Usando a mixagem original.")

        # Sem faixas separadas: prepara a mixagem original
        self.status_box.set_text("Preparando áudio...")
        self._carregando = True
        self._tocar_quando_pronto = tocar
        threading.Thread(
            target=self._preparar_em_segundo_plano,
            args=(self._carga_id, caminho),
            daemon=True,
        ).start()

    def _preparar_em_segundo_plano(self, carga_id, caminho):
        """Roda fora da thread da interface: NÃO mexa nos widgets aqui."""
        wav, erro = preparar_audio_para_player(caminho)
        GLib.idle_add(self._audio_preparado, carga_id, wav, erro)

    def _audio_preparado(self, carga_id, wav, erro):
        # O usuário já escolheu outra música enquanto esta era preparada
        if carga_id != self._carga_id:
            return GLib.SOURCE_REMOVE

        self._carregando = False
        if erro:
            self.status_box.set_text(f"Erro ao preparar o áudio: {erro}")
            return GLib.SOURCE_REMOVE

        ok, mensagem = self.player.carregar(wav)
        if not ok:
            self.status_box.set_text(mensagem)
            return GLib.SOURCE_REMOVE

        self.status_box.set_text(f"Selecionada: {self.musica_atual}")
        if self._tocar_quando_pronto:
            self._tocar_quando_pronto = False
            self.player.tocar()
        return GLib.SOURCE_REMOVE

    def on_play_clicked(self, button):
        if self.player is None:
            self.status_box.set_text(f"Player indisponível: {self._erro_player}")
            return

        # Já carregada: alterna tocar/pausar
        if self.player.carregado:
            if self.player.tocando:
                self.player.pausar()
            else:
                self.player.tocar()
            return

        # Ainda preparando: toca assim que ficar pronta
        if self._carregando:
            self._tocar_quando_pronto = True
            return

        # Nada carregado: usa a música selecionada na lista
        row = self.playlist_listbox.get_selected_row()
        if row is None:
            self.status_box.set_text("Selecione uma música na playlist para tocar.")
            return
        self._carregar_musica(row.get_child().get_text(), tocar=True)

    def on_player_estado(self, tocando):
        self.btn_player_play.set_label("⏸ Pause" if tocando else "▶ Play")

    def on_player_terminou(self):
        # Terminou a música: passa para a próxima da lista, se houver
        self._tocar_vizinha(1)

    def on_player_erro(self, mensagem):
        self.status_box.set_text(f"Erro ao tocar: {mensagem}")

    def on_volume_changed(self, slider):
        if self.player is not None:
            self.player.set_volume(slider.get_value() / 100.0)

    def _tocar_vizinha(self, deslocamento):
        """Seleciona e toca a música anterior (-1) ou próxima (+1). Retorna True se tocou."""
        row = self.playlist_listbox.get_selected_row()
        if row is None:
            return False
        destino = self.playlist_listbox.get_row_at_index(row.get_index() + deslocamento)
        if destino is None:
            return False
        self.playlist_listbox.select_row(destino)  # dispara o carregamento
        if self.player is not None and self.player.carregado:
            self.player.tocar()                     # carregou na hora (mixer de faixas)
        else:
            self._tocar_quando_pronto = True        # toca assim que estiver pronta
        return True

    # ------------------------------------------------------------------
    # Mesa de mixagem (faixas separadas)
    # ------------------------------------------------------------------
    def _definir_mixer_ativo(self, ativo):
        dica = ("" if ativo else
                "Clique em \"Separar Faixas\" para liberar o controle de cada instrumento.")
        for _stem, nome_slider in SLIDERS_STEMS:
            slider = getattr(self, nome_slider)
            slider.set_sensitive(ativo)
            slider.set_tooltip_text(dica or None)

    def _reiniciar_sliders_stems(self):
        """Volta todas as faixas para 100% ao carregar uma música no mixer."""
        for stem, nome_slider in SLIDERS_STEMS:
            slider = getattr(self, nome_slider)
            slider.set_value(100)
            self.player.set_volume_stem(stem, 1.0)

    def on_slider_stem_changed(self, slider, stem):
        if self.player is not None and self.player.modo_stems:
            self.player.set_volume_stem(stem, slider.get_value() / 100.0)

    # ------------------------------------------------------------------
    # Separação de faixas (Demucs)
    # ------------------------------------------------------------------
    def on_btn_processar_clicked(self, button):
        if self._separando:
            return

        nome = self.musica_atual
        if nome is None:
            row = self.playlist_listbox.get_selected_row()
            nome = row.get_child().get_text() if row else None
        if nome is None:
            self.status_processamento.set_text("Selecione uma música na playlist primeiro.")
            return

        # Já separada: só garante que o mixer está carregado
        if stems_prontos(nome):
            self.status_processamento.set_text("Esta música já tem as faixas separadas.")
            if not (self.player and self.player.modo_stems and nome == self.musica_atual):
                self._carregar_musica(nome, tocar=False)
            return

        if not os.path.exists(caminho_da_musica(nome)):
            self.status_processamento.set_text(f"Arquivo não encontrado: {nome}.mp3")
            return

        self._separando = True
        self.btn_processar.set_sensitive(False)
        self.status_processamento.set_text(
            "Iniciando a IA... Isso pode levar alguns minutos (na primeira vez baixa o modelo)."
        )
        self._controle_separacao = ControleSeparacao()
        threading.Thread(
            target=self._separar_em_segundo_plano,
            args=(nome, self._controle_separacao),
            daemon=True,
        ).start()

    def _separar_em_segundo_plano(self, nome, controle):
        """Roda fora da thread da interface: NÃO mexa nos widgets aqui."""
        def progresso(fase, percentual):
            GLib.idle_add(self._mostrar_progresso, fase, percentual)

        sucesso, mensagem = separar_faixas(
            caminho_da_musica(nome), nome, ao_progresso=progresso, controle=controle
        )
        GLib.idle_add(self._separacao_concluida, nome, sucesso, mensagem)

    def _mostrar_progresso(self, fase, percentual):
        if self._separando:
            texto = f"{fase}..."
            if percentual is not None:
                texto = f"{fase}... {percentual}%"
            self.status_processamento.set_text(texto)
        return GLib.SOURCE_REMOVE

    def _separacao_concluida(self, nome, sucesso, mensagem):
        self._separando = False
        self._controle_separacao = None
        self.btn_processar.set_sensitive(True)
        self.status_processamento.set_text(mensagem)

        # Se a música ainda é a selecionada, recarrega já no mixer de faixas
        if sucesso and nome == self.musica_atual:
            self._carregar_musica(nome, tocar=False)
        return GLib.SOURCE_REMOVE
