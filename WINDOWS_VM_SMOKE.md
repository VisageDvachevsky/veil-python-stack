# Windows VM Multi-Client Smoke

Этот smoke нужен для проверки, что Windows client path реально работает против Linux server runtime после включения multi-client handshake.

## Что именно проверяется

- Python bindings на Windows загружаются и устанавливают Veil session.
- `client_id` корректно уходит в handshake из Windows.
- Один Linux server принимает двух Windows clients с разными PSK.
- Reply routing не смешивает сессии и stream ids.

## Что этот smoke не покрывает

- Wintun adapter lifecycle.
- Full-tunnel / route / DNS mutation на Windows.
- GUI / installer path.

Для этого остаётся отдельный ручной прогон из [WINDOWS_VPN.md](WINDOWS_VPN.md).

## Требования к Linux host

- `qemu-system-x86_64`
- `ssh`
- доступ к `/dev/kvm` желателен, но не обязателен

## Требования к Windows guest image

Подразумевается уже подготовленный Windows 10/11 qcow2 image:

- включён OpenSSH Server и доступен вход по ключу или другому non-interactive способу
- установлен Python 3
- в госте уже есть checkout `submodules/veil-python-stack`
- в этом checkout уже собран Windows `_veil_core_ext*.pyd`

Минимальный sanity check внутри гостя:

```powershell
$env:PYTHONPATH='C:\src\veil-core\submodules\veil-python-stack'
py C:\src\veil-core\submodules\veil-python-stack\tests\live_windows_guest_multi_client_smoke.py --host 127.0.0.1 --port 4433
```

## Запуск через QEMU/KVM

```bash
python3 tests/live_windows_vm_multi_client_smoke.py \
  --launch-qemu \
  --vm-image /vm/windows11.qcow2 \
  --enable-kvm \
  --guest-user veil \
  --guest-repo-root 'C:\src\veil-core\submodules\veil-python-stack' \
  --ssh-identity ~/.ssh/veil-windows
```

Что делает orchestrator:

- поднимает локальный Linux server на случайном UDP порту
- стартует Windows VM через QEMU user networking
- ждёт SSH на `127.0.0.1:2222`
- запускает в госте `tests/live_windows_guest_multi_client_smoke.py`
- сохраняет логи в `/tmp/veil-windows-vm-*.log`

Хостовый server bindится только на high UDP port и не меняет routes/VPN stack хоста.

## Запуск против уже работающей VM

```bash
python3 tests/live_windows_vm_multi_client_smoke.py \
  --guest-user veil \
  --guest-repo-root 'C:\src\veil-core\submodules\veil-python-stack' \
  --ssh-host 127.0.0.1 \
  --ssh-port 2222 \
  --ssh-identity ~/.ssh/veil-windows \
  --server-host 10.0.2.2
```

`--server-host 10.0.2.2` подходит для QEMU user-mode networking. Если используется bridge/tap, укажи адрес Linux host, который видит Windows VM.
