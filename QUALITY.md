# Quality And Metrics

Этот документ про качество transport stack, а не про API usage.

Коротко:
- `bindings/python/metrics/latest_quality_metrics.json` это текущий слепок метрик
- `bindings/python/metrics/latest_quality_metrics_clean_release.json` это clean-release слепок без sanitizer/perf diagnostics overhead
- рядом с clean-release JSON теперь лежат CSV с плоскими distribution-рядами по ключевым сериям
- full-stack метрики снимаются через реальные `Server` / `Client` wrappers
- protocol-only метрики снимаются из C++ integration binary
- `asan_sanity_core_payload_throughput_mb_s` нельзя читать как "максимальная скорость протокола"
- для более честной верхней границы есть отдельный `release_like_core_payload_throughput_mbps`

## Где лежит JSON

- [latest_quality_metrics.json](metrics/latest_quality_metrics.json)
- [latest_quality_metrics_clean_release.json](metrics/latest_quality_metrics_clean_release.json)

## Где лежат CSV с распределениями

Exporter теперь пишет не только JSON, но и плоские CSV рядом с output-файлом:

- [latest_quality_metrics_clean_release_handshake_distribution.csv](metrics/latest_quality_metrics_clean_release_handshake_distribution.csv)
- [latest_quality_metrics_clean_release_sustained_runs.csv](metrics/latest_quality_metrics_clean_release_sustained_runs.csv)
- [latest_quality_metrics_clean_release_windowed_runs.csv](metrics/latest_quality_metrics_clean_release_windowed_runs.csv)
- [latest_quality_metrics_clean_release_ingress_pacing_runs.csv](metrics/latest_quality_metrics_clean_release_ingress_pacing_runs.csv)

Практический смысл:
- JSON удобно читать кодом и использовать для summary
- CSV удобно открывать в Excel, LibreOffice, pandas, Grafana import, CI artifacts и для быстрой визуализации

## Как обновить JSON

Windows + WSL:

```powershell
wsl -e bash -lc '
  cd /mnt/c/Users/Ya/OneDrive/Desktop/veil-coreeee &&
  export LD_PRELOAD=/usr/lib/gcc/x86_64-linux-gnu/13/libasan.so &&
  export ASAN_OPTIONS=detect_leaks=0 &&
  export PYTHONPATH=/mnt/c/Users/Ya/OneDrive/Desktop/veil-coreeee/bindings/python &&
  python3 bindings/python/tests/export_quality_metrics.py \
    --output /mnt/c/Users/Ya/OneDrive/Desktop/veil-coreeee/bindings/python/metrics/latest_quality_metrics.json
'
```

Если нужен clean-release baseline, используй output:

```bash
bindings/python/metrics/latest_quality_metrics_clean_release.json
```

CSV появятся автоматически рядом с ним.

## Что именно меряется

### Binding + Protocol E2E

Источник:
- [e2e_metrics.py](tests/e2e_metrics.py)

Основные тесты:
- `full_stack_roundtrip`: cold connect + request/response RTT
- `full_stack_reconnect`: disconnect propagation и повторный handshake
- `full_stack_stream_fanout`: preservation `stream_id` и multi-message flow
- `full_stack_payload_sweep`: latency и effective payload rate по размерам payload
- `full_stack_sustained_throughput`: короткий burst throughput без window management
- `full_stack_windowed_throughput_sweep`: throughput под управляемым in-flight окном
- `handshake_distribution`: распределение handshake latency по повторениям
- `full_stack_stability_series`: повторные прогоны sustained/windowed сценариев для оценки jitter и стабильности

### Protocol-only

Источник:
- [transport_integration.cpp](../../tests/integration/transport_integration.cpp)

Основные quality-тесты:
- `LoopbackRoundTripLatencyMetricsStaySane`
- `CoreEncryptDecryptThroughputMetricsStaySane`

## Как читать цифры

### `asan_sanity_core_payload_throughput_mb_s`

Это не throughput всего стека.

Это маленький in-memory C++ sanity-test для encrypt/decrypt core под ASan. Он полезен как regression guard:
- если было `6.8 MB/s`, а стало `2 MB/s`, значит core деградировал
- если full-stack плохой, а core sanity stable, bottleneck уже не в crypto/session core

### `release_like_core_payload_throughput_mbps`

Это уже не ASan sanity-check, а отдельный release-like benchmark из `veil-performance-validation`.

Его нужно читать как более близкую к реальности оценку throughput crypto/session core без Python wrappers и без тяжёлого sanitizer overhead.

Практический смысл:
- если ASan sanity metric низкая, но release-like benchmark высокий, значит headline-цифру душит sanitizer/runtime overhead
- если и release-like benchmark низкий, значит bottleneck уже сидит в самом core path
- если release-like benchmark высокий, а full-stack throughput низкий, значит проблема выше: UDP path, queueing, pacing, fragmentation, ACK/window logic

### `sustained_payload_mb_s`

Это лучше, чем core metric, потому что тут уже работают:
- Python wrapper
- pybind boundary
- UDP path
- session routing
- pipeline

Но это всё ещё burst scenario, а не долгий steady-state канал.

### `full_stack_windowed_throughput_sweep`

Это сейчас самая полезная full-stack throughput метрика.

Она показывает по каждому `payload_size`:
- `payload_mb_s`
- `payload_mbit_s`
- `ack_loss_ratio`
- `ack_delivery_ratio`
- `message_delivery_ratio`
- `fragment_delivery_ratio`
- `message_reassembly_ratio`
- `ack_latency_ms`
- `transport_efficiency`
- wire bytes на tx/rx

Если тут есть loss, значит проблема уже в queueing / pacing / pipeline behavior, а не просто в headline throughput.

### Ошибочные счётчики

В `stats()` теперь важно различать:
- `decrypt_errors`: только реальные RX decrypt failures
- `tx_crypto_errors`: send-side crypto/encrypt failures
- `callback_errors`: исключения из callback path
- `processing_exceptions`: прочие pipeline processing exceptions

Это важно, потому что раньше `decrypt_errors` был загрязнён всеми видами pipeline-ошибок и мог давать ложный красный флаг.

### `transport_efficiency`

Это отношение полезной payload-нагрузки к реальным wire bytes на client TX.

Пример:
- `0.96` значит примерно 96% wire bytes уходит на payload и 4% на overhead
- низкое значение на маленьких payload нормально

### Delivery ratios

Для sustained/windowed сценариев теперь важно смотреть не только на `ack_loss_ratio`, но и на:
- `ack_delivery_ratio`: доля отправленных сообщений, для которых клиент реально получил ACK/data-response
- `message_delivery_ratio`: доля сообщений, которые сервер реально поднял до `DataEvent`
- `fragment_delivery_ratio`: сколько transport fragments дошло до server-side receive path относительно реально отправленных fragments
- `message_reassembly_ratio`: сколько полных сообщений сервер реально собрал из fragments

Для repeated-run анализа полезны ещё:
- `payload_mb_s.cv`: коэффициент вариации throughput между повторениями
- `stability_worst_windowed_payload_cv` в summary: худший разброс по windowed-сериям
- `stability_noisiest_windowed_payload_size`: какой payload bucket шумит сильнее всего
- `stability_noisiest_ingress_pacing_config`: какой ingress pacing config даёт худший разброс
- `system_probes` в environment: socket buffers, clock resolution, loop type и прочий контекст среды

Интерпретация:
- `decrypt_errors = 0`, но падает `fragment_delivery_ratio`:
  проблема выглядит как недоставка/перегрузка receive path, а не crypto failure
- `fragment_delivery_ratio` высокий, но падает `message_delivery_ratio`:
  проблема смещается в decode/reassembly/event-delivery path
- `fragment_delivery_ratio` высокий, а `message_reassembly_ratio` низкий:
  проблема уже в fragmented reassembly/ordering path
- `message_delivery_ratio` высокий, а `ack_delivery_ratio` падает:
  bottleneck смещается в server response / client receive / ACK path

## Что видно по текущему слепку

По текущему clean-release слепку [latest_quality_metrics_clean_release.json](metrics/latest_quality_metrics_clean_release.json) summary надо читать так:

- `handshake_ms` и `roundtrip_p95_ms` показывают холодный connect и steady-state latency
- `sustained_payload_mb_s` показывает burst full-stack throughput без window management
- `best_windowed_payload_mb_s` и `best_windowed_payload_mbit_s` показывают лучший bucket под управляемым окном
- `asan_sanity_core_payload_throughput_mb_s` это sanitizer regression guard для core path
- `release_like_core_payload_throughput_mbps` это более честная release-like baseline для core path
- `fragment_probe_max_*` счётчики показывают, были ли corruption/decode/fragmentation pathologies в отдельном targeted probe

Это означает:
- маленькая цифра ASan sanity throughput сама по себе не отражает весь стек
- реальный bottleneck сейчас не в payload overhead, потому что efficiency хорошая
- свежий targeted probe должен оставаться с нулевыми corruption/drop counters
- текущая деградация больше похожа на delivery/back-pressure проблему под нагрузкой

По свежему clean-release прогону ориентиры сейчас такие:
- `handshake_ms` около `9 ms`
- `best_windowed_payload_mb_s` около `51 MB/s`
- самый шумный `windowed` bucket: `4096`, `cv ≈ 0.020`
- самый шумный `ingress pacing` config: `paced_100us_batch4`, `cv ≈ 0.027`

Это хороший знак:
- `cv` порядка `2-3%` уже больше похоже на нормальный run-to-run jitter, а не на нестабильный transport path
- если начнём видеть `cv` сильно выше `0.10` на clean-release сериях, это уже повод считать сценарий шумным или деградировавшим

## Что считать regression

Плохой признак:
- рост `handshake_distribution_p95_ms`
- рост `roundtrip_p95_ms`
- рост `ack_loss_ratio` в `full_stack_windowed_throughput_sweep`
- появление `decrypt_errors`
- падение `transport_efficiency` на больших payload

Хороший признак:
- `ack_loss_ratio` идёт к нулю
- `best_windowed_payload_mb_s` растёт
- `best_windowed_ack_p95_ms` остаётся низким
- `asan_sanity_core_payload_throughput_mb_s` не деградирует
- `release_like_core_payload_throughput_mbps` не деградирует
- `stability_noisiest_windowed_payload_cv` и `stability_noisiest_ingress_pacing_payload_cv` остаются низкими

## Как отличать код от среды

Сигналы в пользу проблем кода:
- `decrypt_errors` или `transport_packets_dropped_decrypt/replay` растут повторяемо
- одни и те же payload buckets стабильно деградируют при низком `cv`
- и ASan sanity metric, и release-like core benchmark падают вместе с full-stack цифрами

Сигналы в пользу среды или этапа реализации:
- decrypt/replay counters чистые, а delivery/throughput заметно плавают между повторами
- `payload_mb_s.cv` высокий, но медиана/avg остаются приемлемыми
- деградация проявляется в основном на больших fragmented payload
- `system_probes` указывают на маленькие socket buffers, высокий ASan overhead или нестабильную среду запуска

## Что делать дальше

Если цель именно поднять качество transport path, следующая работа должна быть не в Python wrappers, а в C++ transport/pipeline:
- pacing / batching под sustained load
- queue sizing и back-pressure policy
- причина `decrypt_errors` под load
- причина потери ACK-ов в windowed sweep

Python-разработчику эти цифры уже дают честную картину:
- API живой
- round-trip живой
- handshake живой
- sustained path работает
- под нагрузкой ещё есть деградация, и она локализуется в transport path

## Какой CSV за что отвечает

`*_handshake_distribution.csv`
- одна строка на повтор handshake
- полезен для histogram/boxplot по cold-connect latency

`*_sustained_runs.csv`
- одна строка на sustained прогон
- полезен для оценки burst throughput и jitter между сериями

`*_windowed_runs.csv`
- одна строка на конкретный run внутри конкретного `payload_size`
- лучший файл для анализа стабильности throughput/ACK path по payload buckets

`*_ingress_pacing_runs.csv`
- одна строка на run внутри pacing-конфига
- нужен, чтобы отделять проблемы transport path от проблем конкретного send pattern
