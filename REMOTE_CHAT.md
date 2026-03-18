# Remote Chat

Текущий браузерный чат работает так:

- браузер показывает только UI
- реальный Veil transport живёт в локальном процессе `examples/local_chat.py`

Значит для использования через интернет:

- у сервера должен быть запущен `local_chat.py --mode server`
- у клиента на удалённом ПК должен быть запущен `local_chat.py --mode client`

## Сервер

Открыть порты:

- `UDP 4433` для Veil
- `TCP 8080` для browser UI

Запуск:

```bash
cd submodules/veil-python-stack
PYTHONPATH=. python3 examples/local_chat.py \
  --mode server \
  --host 0.0.0.0 \
  --veil-port 4433 \
  --ui-host 0.0.0.0 \
  --ui-port 8080 \
  --name server
```

UI сервера:

- локально: `http://127.0.0.1:8080`
- в LAN: `http://<LAN-IP>:8080`
- извне: `http://<external-ip>:8080`, если открыт `TCP 8080`

## Удалённый клиент

Windows:

```powershell
git clone --recurse-submodules https://github.com/VisageDvachevsky/veil-core.git
cd veil-core\submodules\veil-python-stack
python -m pip install aiohttp
$env:PYTHONPATH="."
python examples\local_chat.py --mode client --host <SERVER_EXTERNAL_IP> --veil-port 4433 --ui-host 127.0.0.1 --ui-port 8081 --name friend
```

Linux:

```bash
git clone --recurse-submodules https://github.com/VisageDvachevsky/veil-core.git
cd veil-core/submodules/veil-python-stack
python3 -m pip install aiohttp
PYTHONPATH=. python3 examples/local_chat.py \
  --mode client \
  --host <SERVER_EXTERNAL_IP> \
  --veil-port 4433 \
  --ui-host 127.0.0.1 \
  --ui-port 8081 \
  --name friend
```

Открыть у клиента:

- `http://127.0.0.1:8081`

## Важно

- серверу нужен белый IP или проброс портов
- клиенту нужен тот же `psk`, что уже зашит по умолчанию в demo
- одного браузера без локального процесса сейчас недостаточно
- если у клиента не собран `veil_core._veil_core_ext`, нужно отдельно собирать Python binding
