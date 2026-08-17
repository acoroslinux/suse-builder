#!/bin/bash
# Script para instalar as Ações Personalizadas mais utilizadas no Thunar (Xfce)

CONFIG_DIR="$HOME/.config/Thunar"
CONFIG_FILE="$CONFIG_DIR/uca.xml"

# Garante que o diretório existe
mkdir -p "$CONFIG_DIR"

# Instala dependências comuns para as ações funcionarem (opcional, requer sudo)
echo "Instalando ferramentas de suporte (xclip, imagemagick)..."
if command -v apt-get &> /dev/null; then
    sudo apt-get update && sudo apt-get install -y xclip imagemagick
elif command -v pacman &> /dev/null; then
    sudo pacman -Sy --needed xclip imagemagick
fi

# Backup do arquivo atual se ele existir
if [ -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak_$(date +%F_%R)"
    echo "Backup do uca.xml atual criado em $CONFIG_FILE.bak_..."
fi

# Cria o novo arquivo uca.xml
cat << 'EOF' > "$CONFIG_FILE"
<?xml id="1.0" encoding="UTF-8"?>
<actions>
<action>
	<icon>utilities-terminal</icon>
	<name>Abrir Terminal Aqui</name>
	<subnet-mask></subnet-mask>
	<unique-id>1718000000000001</unique-id>
	<command>xfce4-terminal --working-directory=%f</command>
	<description>Abre o terminal na pasta atual</description>
	<patterns>*</patterns>
	<directories/>
</action>
<action>
	<icon>gksu-root-terminal</icon>
	<name>Abrir como Administrador</name>
	<subnet-mask></subnet-mask>
	<unique-id>1718000000000002</unique-id>
	<command>pkexec thunar %f</command>
	<description>Abre a pasta com privilégios root</description>
	<patterns>*</patterns>
	<directories/>
</action>
<action>
	<icon>edit-copy</icon>
	<name>Copiar Caminho</name>
	<subnet-mask></subnet-mask>
	<unique-id>1718000000000003</unique-id>
	<command>echo -n %f | xclip -selection clipboard</command>
	<description>Copia o caminho absoluto para a área de transferência</description>
	<patterns>*</patterns>
	<directories/>
	<audio-files/>
	<image-files/>
	<other-files/>
	<text-files/>
	<video-files/>
</action>
<action>
	<icon>image-jpeg</icon>
	<name>Converter para PNG</name>
	<subnet-mask></subnet-mask>
	<unique-id>1718000000000004</unique-id>
	<command>convert %f %f.png</command>
	<description>Converte imagem usando o ImageMagick</description>
	<patterns>*.jpg;*.jpeg;*.webp;*.bmp</patterns>
	<image-files/>
</action>
</actions>

EOF

echo "Ações personalizadas instaladas com sucesso!"
echo "Por favor, reinicie o Thunar rodando 'thunar -q' para aplicar as alterações."
