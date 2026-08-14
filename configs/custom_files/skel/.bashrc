# ~/.bashrc for openSUSE Modern

# If not running interactively, don't do anything
[[ $- != *i* ]] && return

alias ls='ls --color=auto'
alias ll='ls -la --color=auto'
alias la='ls -A --color=auto'
alias l='ls -CF --color=auto'
alias grep='grep --color=auto'
alias fgrep='fgrep --color=auto'
alias egrep='egrep --color=auto'
alias cls='clear'

# Custom prompt
export PS1='\[\033[01;32m\]liveuser@opensuse-modern\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '

# Display system info if fastfetch or neofetch is installed
if command -v fastfetch &> /dev/null; then
    fastfetch
elif command -v neofetch &> /dev/null; then
    neofetch
fi
