#!/bin/sh
# Configuração crítica para Gentoo Linux Live: Inicia a sessão dentro do DBUS
# para que o Polkit, pkexec e o Elogind funcionem sem ecrãs em branco.

# Disable screen locking and screensaver for the Live environment
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/lock-screen-suspend-hibernate -n -t bool -s false 2>/dev/null || true
xfconf-query -c xfce4-session -p /general/LockCommand -n -t string -s "" 2>/dev/null || true
xset s off -dpms 2>/dev/null || true

exec dbus-run-session startxfce4
