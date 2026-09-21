#!/usr/bin/env bash
# Первичная защита сервера 85.217.171.186 (Debian 12).
#
# Состояние на момент написания: root-вход по паролю, fail2ban нет,
# firewall не настроен, ключей в /root/.ssh нет.
#
# Скрипт разбит на два этапа намеренно. Между ними вы обязаны своими глазами
# убедиться, что новый способ входа работает. Всё, что может отрезать доступ
# (закрытие 22-го порта, отключение парольного входа), вынесено во второй этап
# и не выполнится, пока первый не проверен.
#
#   ЭТАП 1  ставит ключ, fail2ban и открывает новый порт ДОПОЛНИТЕЛЬНО к 22
#   проверка вы открываете вторую сессию на новом порту по ключу
#   ЭТАП 2  убирает 22-й порт, выключает вход по паролю, поднимает firewall
#
# Запуск на сервере:
#   bash harden-server.sh phase1
#   bash harden-server.sh phase2
set -euo pipefail

SSH_PORT="${SSH_PORT:-52022}"
DROPIN=/etc/ssh/sshd_config.d/99-hardening.conf

die() { echo "ОШИБКА: $*" >&2; exit 1; }
say() { echo; echo "── $*"; }

[ "$(id -u)" -eq 0 ] || die "нужен root"

# ---------------------------------------------------------------------------

phase1() {
  say "Проверки перед началом"
  ss -tlnp | grep -q ":${SSH_PORT}\b" && die "порт ${SSH_PORT} уже занят"

  if [ ! -s /root/.ssh/authorized_keys ]; then
    cat >&2 <<'MSG'
В /root/.ssh/authorized_keys пусто — сначала положите туда свой публичный ключ,
иначе после второго этапа в систему будет не войти.

На своей машине:
    ssh-keygen -t ed25519 -C "ваша-почта"
    ssh-copy-id root@85.217.171.186

Затем запустите скрипт заново.
MSG
    exit 1
  fi
  echo "ключей в authorized_keys: $(grep -c . /root/.ssh/authorized_keys)"
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/authorized_keys

  say "Установка fail2ban и nftables"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # nftables нужен как механизм блокировки: iptables в системе нет, а fail2ban
  # в Debian 12 по умолчанию зовёт именно iptables и молча не банит никого
  apt-get install -y -qq fail2ban nftables

  say "Настройка fail2ban"
  cat > /etc/fail2ban/jail.local <<EOF
[DEFAULT]
# банить надолго: у легитимного пользователя есть ключ, ошибаться ему нечем
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd
# блокировка через nftables: iptables в системе не установлен
banaction = nftables-multiport
banaction_allports = nftables-allports

[sshd]
enabled = true
port    = ${SSH_PORT},22
EOF
  systemctl enable --now fail2ban
  sleep 2
  fail2ban-client status sshd || die "fail2ban не поднялся"

  say "Открываю порт ${SSH_PORT} дополнительно к 22"
  cat > "$DROPIN" <<EOF
# Новый порт добавлен рядом с 22. Двадцать второй уберёт этап 2 — после того,
# как вы убедитесь, что вход на ${SSH_PORT} работает.
Port 22
Port ${SSH_PORT}
EOF
  sshd -t || die "конфигурация sshd невалидна, ничего не меняю"
  systemctl reload ssh
  sleep 1
  ss -tlnp | grep -q ":${SSH_PORT}\b" || die "sshd не слушает ${SSH_PORT}"

  cat <<MSG

╔════════════════════════════════════════════════════════════════════╗
║  ЭТАП 1 ЗАВЕРШЁН. Текущую сессию НЕ ЗАКРЫВАЙТЕ.                   ║
╚════════════════════════════════════════════════════════════════════╝

Откройте ВТОРОЕ окно терминала и проверьте вход по ключу на новом порту:

    ssh -p ${SSH_PORT} root@85.217.171.186

Только если вход удался — вернитесь сюда и запустите:

    bash $0 phase2

Если не удался — ничего не делайте, доступ на 22 по паролю ещё работает.
MSG
}

# ---------------------------------------------------------------------------

phase2() {
  say "Проверки перед закрытием доступа"

  # Ключ обязан быть: иначе после выключения пароля войти будет нечем
  [ -s /root/.ssh/authorized_keys ] || die "authorized_keys пуст — вход будет потерян"

  # Убеждаемся, что эта сессия пришла на новый порт: если вы всё ещё сидите
  # на 22-м, закрытие 22-го оборвёт вас прямо сейчас
  local port
  port=$(ss -tnp 2>/dev/null | awk -v p="$PPID" '/sshd/ {print $4}' | sed 's/.*://' | head -1)
  if [ "${port:-}" = "22" ]; then
    die "вы подключены на 22-м порту. Перезайдите: ssh -p ${SSH_PORT} root@85.217.171.186"
  fi

  say "Закрываю 22-й порт и вход по паролю"
  cat > "$DROPIN" <<EOF
Port ${SSH_PORT}

# Вход только по ключу. Пароль root скомпрометирован тем, что пересылался
# в переписке, и парольный вход обязан быть выключен.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no

# Меньше поверхности: агент и проброс X11 на этом сервере не нужны
AllowAgentForwarding no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 20
EOF
  sshd -t || die "конфигурация sshd невалидна, ничего не меняю"
  systemctl reload ssh
  sleep 1
  ss -tlnp | grep -q ":${SSH_PORT}\b" || die "sshd не слушает ${SSH_PORT}"

  say "Firewall"
  # Порядок важен: правило для SSH добавляется ДО политики drop, иначе
  # соединение оборвётся на середине настройки
  cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;

    ct state established,related accept
    ct state invalid drop
    iif lo accept

    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept

    tcp dport ${SSH_PORT} accept
    # HTTP/HTTPS открыты заранее: сайт сюда ещё не выложен, но открывать
    # порты позже, уже под политикой drop, придётся снова редактируя файл
    tcp dport { 80, 443 } accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output  priority 0; policy accept; }
}
EOF
  nft -c -f /etc/nftables.conf || die "правила nftables невалидны"
  systemctl enable --now nftables
  nft -f /etc/nftables.conf

  say "Перезапуск fail2ban под новые правила"
  sed -i "s/^port    = .*/port    = ${SSH_PORT}/" /etc/fail2ban/jail.local
  systemctl restart fail2ban
  sleep 2

  say "Итог"
  echo "порт SSH        : ${SSH_PORT}"
  echo "вход по паролю  : выключен"
  echo "firewall        : $(systemctl is-active nftables)"
  echo "fail2ban        : $(systemctl is-active fail2ban)"
  fail2ban-client status sshd | sed 's/^/  /'
  cat <<MSG

Осталось сделать вручную, скриптом это делать нельзя:

  1. Сменить пароль root — старый утёк:   passwd
     Он больше не пускает в SSH, но остаётся паролем консоли VNC у хостера.
  2. Проверить, что вход работает из нового окна:
       ssh -p ${SSH_PORT} root@85.217.171.186
MSG
}

case "${1:-}" in
  phase1) phase1 ;;
  phase2) phase2 ;;
  *) echo "Использование: $0 phase1|phase2   (порт задаётся через SSH_PORT=)"; exit 2 ;;
esac
