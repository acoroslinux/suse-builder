#!/bin/bash
# Script para adicionar mais ações customizadas avançadas ao Thunar (Xfce)

UCA_FILE="$HOME/.config/Thunar/uca.xml"

# Criar pasta de configuração caso não exista
mkdir -p "$(dirname "$UCA_FILE")"

# Se o ficheiro não existir, inicia com a estrutura XML correta
if [ ! -f "$UCA_FILE" ]; then
    echo '<?xml version="1.0" encoding="UTF-8"?>' > "$UCA_FILE"
    echo '<actions>' >> "$UCA_FILE"
    echo '</actions>' >> "$UCA_FILE"
fi

# Função para injetar ações antes do fecho da tag </actions>
adicionar_acao() {
    local acao="$1"
    # Remove a tag de fecho para anexar e depois fecha novamente
    sed -i '/<\/actions>/d' "$UCA_FILE"
    echo "$acao" >> "$UCA_FILE"
    echo "</actions>" >> "$UCA_FILE"
}

# 1. CRIAR LINK SIMBÓLICO
ACAO_SYMLINK="<action>
	<icon>link</icon>
	<name>Criar Link Simbólico</name>
	<unique-id>$(date +%s)-symlink</unique-id>
	<command>ln -s %f \"%f (link)\"</command>
	<description>Criar um link simbólico do ficheiro atual</description>
	<patterns>*</patterns>
	<directories/>
	<audio-files/>
	<image-files/>
	<other-files/>
	<text-files/>
	<video-files/>
</action>"
adicionar_acao "$ACAO_SYMLINK"
sleep 1

# 2. CALCULAR VERIFICAÇÃO SHA256
ACAO_SHA256="<action>
	<icon>dialog-password</icon>
	<name>Calcular SHA256</name>
	<unique-id>$(date +%s)-sha256</unique-id>
	<command>sha256sum %f | zenity --text-info --title=\"Checksum SHA256\" --width=600 --height=100</command>
	<description>Verificar a integridade do ficheiro</description>
	<patterns>*</patterns>
	<audio-files/>
	<image-files/>
	<other-files/>
	<text-files/>
	<video-files/>
</action>"
adicionar_acao "$ACAO_SHA256"
sleep 1

# 3. REDIMENSIONAR IMAGEM (50%)
ACAO_RESIZE="<action>
	<icon>image-jpeg</icon>
	<name>Redimensionar Imagem (50%)</name>
	<unique-id>$(date +%s)-resize</unique-id>
	<command>convert %f -resize 50%% \"%f_reduzida.png\"</command>
	<description>Reduzir dimensões da imagem pela metade</description>
	<patterns>*.jpg;*.jpeg;*.png;*.webp</patterns>
	<image-files/>
</action>"
adicionar_acao "$ACAO_RESIZE"
sleep 1

# 4. EXTRAIR ÁUDIO DE VÍDEO (MP3)
ACAO_MP3="<action>
	<icon>audio-x-generic</icon>
	<name>Extrair Áudio (MP3)</name>
	<unique-id>$(date +%s)-mp3</unique-id>
	<command>ffmpeg -i %f -vn -ar 44100 -ac 2 -b:a 192k \"%f.mp3\"</command>
	<description>Converter faixa de vídeo para MP3</description>
	<patterns>*</patterns>
	<video-files/>
</action>"
adicionar_acao "$ACAO_MP3"
sleep 1

# 5. EDITAR COMO ROOT (TEXTO)
ACAO_EDIT_ROOT="<action>
	<icon>accessories-text-editor</icon>
	<name>Editar como Root</name>
	<unique-id>$(date +%s)-editroot</unique-id>
	<command>pkexec mousepad %f</command>
	<description>Abrir ficheiro de configuração como administrador</description>
	<patterns>*</patterns>
	<text-files/>
</action>"
adicionar_acao "$ACAO_EDIT_ROOT"

echo "Instalação concluída! Novas ações adicionadas com sucesso."
