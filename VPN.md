# Veil VPN Overlay

Сейчас в `veil_core.vpn` появился отдельный overlay поверх `Session`.

Это уже не чатовый demo-протокол, а базовая клиент-серверная обвязка для
packet-oriented туннеля:

- `VpnClient`
- `VpnServer`
- `VpnConnection`
- `LinuxTunDevice`
- `LinuxVpnProxyClient`
- `LinuxVpnProxyServer`
- keepalive / timeout на control channel
- reconnect loop у Linux client proxy

## Что делает overlay

- поднимает session-level handshake поверх Veil transport:
  - `vpn.hello`
  - `vpn.ready`
  - `vpn.close`
- резервирует отдельные stream id:
  - control stream: `1`
  - packet stream: `2`
- передаёт packet payload как raw bytes
- согласует packet MTU между клиентом и сервером
- умеет ловить `DisconnectedEvent` и закрывать VPN connection корректно

## Минимальный пример

```python
import asyncio
from veil_core import VpnClient, VpnServer

PSK = bytes.fromhex("ab" * 32)


async def main() -> None:
    server = VpnServer(port=4433, host="127.0.0.1", psk=PSK, local_name="edge")
    client = VpnClient(host="127.0.0.1", port=4433, psk=PSK, local_name="laptop")

    async with server, client:
        accept_task = asyncio.create_task(server.accept())
        client_conn = await client.connect()
        server_conn = await accept_task

        client_conn.send_packet(b"ipv4-packet-bytes")
        packet = await server_conn.recv_packet(timeout=5.0)
        print(packet.payload)

        await client_conn.close("done")
        await server_conn.wait_closed(timeout=5.0)


asyncio.run(main())
```

## Linux TUN proxy

Для Linux есть отдельный раннер:

- `examples/linux_vpn_proxy.py`
- реальный smoke: `tests/live_linux_tun_smoke.py`
- full-tunnel helper:
  - `examples/linux_vpn_full_tunnel_up.sh`
  - `examples/linux_vpn_full_tunnel_down.sh`
- GUI client:
  - `desktop/veil_vpn_client.py`
  - `desktop/veil_vpn_client.example.json`
- CLI controller / installer:
  - `desktop/veil_vpn_ctl.py`
  - `desktop/install_linux_client.py`
- provisioning:
  - `desktop/veil_vpn_server_ctl.py`
  - `desktop/veil_vpn_server.py`
- deploy scripts:
  - `deploy/install_linux_server.sh`
  - `deploy/install_linux_client.sh`

Пример:

```bash
# server
PYTHONPATH=. python3 examples/linux_vpn_proxy.py \
  --mode server \
  --host 0.0.0.0 \
  --port 4433 \
  --tun-name veil0 \
  --tun-address 10.200.0.1/30 \
  --tun-peer 10.200.0.2 \
  --route 10.210.0.0/24

# client
PYTHONPATH=. python3 examples/linux_vpn_proxy.py \
  --mode client \
  --host <SERVER_IP> \
  --port 4433 \
  --tun-name veil0 \
  --tun-address 10.200.0.2/30 \
  --tun-peer 10.200.0.1 \
  --route 10.220.0.0/24
```

Нужны права root и установленный `iproute2`.

Быстрая живая проверка на одной Linux-машине:

```bash
python3 tests/live_linux_tun_smoke.py
```

Скрипт сам поднимает два `ip netns`, underlay `veth`, два процесса Veil proxy и
проверяет `ping` через TUN.

## Full tunnel on client

Если нужно временно перевести Linux-клиента в full-tunnel режим, используй:

```bash
cd submodules/veil-python-stack
sudo SERVER_HOST=vpn.example TUN_NAME=veilfull0 bash examples/linux_vpn_full_tunnel_up.sh
```

Скрипт:

- сохраняет underlay-route до сервера
- поднимает отдельный Veil client process
- добавляет split-default:
  - `0.0.0.0/1`
  - `128.0.0.0/1`

Остановить и вернуть маршруты:

```bash
cd submodules/veil-python-stack
sudo TUN_NAME=veilfull0 bash examples/linux_vpn_full_tunnel_down.sh
```

## GUI client and installer

Установка пользовательских launcher’ов:

```bash
cd submodules/veil-python-stack
python3 desktop/install_linux_client.py
```

Это создаёт:

- config: `~/.config/veil-vpn/client.json`
- CLI wrapper: `~/.local/bin/veil-vpn`
- GUI wrapper: `~/.local/bin/veil-vpn-gui`
- desktop entry: `~/.local/share/applications/veil-vpn.desktop`

Проверка окружения:

```bash
veil-vpn doctor
```

Статус клиента:

```bash
veil-vpn status
```

Поднять VPN:

```bash
veil-vpn up
```

Остановить VPN:

```bash
veil-vpn down
```

GUI:

```bash
veil-vpn-gui
```

GUI использует тот же config-файл и те же `up/down` действия, что и CLI.

## Provisioning flow

Серверный install:

```bash
cd submodules/veil-python-stack
sudo PUBLIC_HOST=vpn.example PROFILE_OUT=/root/veil-client-profile.json \
  bash deploy/install_linux_server.sh
```

Что делает серверный install:

- генерирует случайный `psk`
- пишет server config
- готовит client profile JSON
- ставит launcher и `systemd` unit
- включает `veil-vpn-server.service`

Клиентский install:

```bash
cd submodules/veil-python-stack
PROFILE_PATH=/path/to/veil-client-profile.json bash deploy/install_linux_client.sh
```

Или потом отдельно:

```bash
veil-vpn import-profile --profile /path/to/veil-client-profile.json
```

После этого клиент уже знает:

- к какому серверу подключаться
- какой `psk` использовать
- какой `protocol_wrapper` и `persona_preset` применять

Можно использовать и однотокенный provisioning flow:

```bash
cd submodules/veil-python-stack
python3 desktop/veil_vpn_server_ctl.py export-client-token
veil-vpn import-profile --profile-token 'veil://profile/...'
```

GUI-клиент тоже умеет импортировать такой токен через `Import Token`.

## GitHub bootstrap

Сервер можно ставить прямо из GitHub-скрипта:

```bash
curl -fsSL https://raw.githubusercontent.com/VisageDvachevsky/veil-core/main/deploy/bootstrap_install_linux_server.sh | \
  sudo PUBLIC_HOST=vpn.example bash
```

Клиент:

```bash
curl -fsSL https://raw.githubusercontent.com/VisageDvachevsky/veil-core/main/deploy/bootstrap_install_linux_client.sh | \
  PROFILE_TOKEN='veil://profile/...' bash
```

Bootstrap-скрипты сами:

- определяют пакетный менеджер
- могут скачать готовый Linux artifact через `ARTIFACT_PATH` / `ARTIFACT_URL` / `ARTIFACT_BASE_URL`
- если artifact не задан, подтягивают или обновляют репозиторий
- инициализируют сабмодули
- создают `venv`
- собирают Python binding только как fallback
- ставят server/client launcher’ы
- на сервере поднимают `systemd`-service

Сборка artifact:

```bash
cd veil-coreeee
PYTHON_BIN=python3 deploy/build_linux_artifact.sh
```

Установка сервера из локального artifact:

```bash
curl -fsSL https://raw.githubusercontent.com/VisageDvachevsky/veil-core/main/deploy/bootstrap_install_linux_server.sh | \
  sudo PUBLIC_HOST=vpn.example ARTIFACT_PATH=/root/veil-linux-x86_64-cp312.tar.gz bash
```

## Что это ещё не делает

Это пока не полноценный OS-level VPN.

Ещё нет:

- TUN/TAP адаптера
- route management
- DNS handling
- NAT / forwarding policy
- keepalive / reconnect supervisor
- packet capture from kernel interface

## Следующий логичный шаг

Подключить `VpnConnection` к platform-specific TUN backend:

- Linux: `/dev/net/tun`
- Windows: Wintun

Тогда `recv_packet()` и `send_packet()` станут мостом между Veil session и
виртуальным сетевым интерфейсом ОС.
