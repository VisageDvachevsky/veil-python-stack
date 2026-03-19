#include "node.h"

#include <chrono>
#include <stdexcept>
#include <system_error>

#include "common/handshake/handshake_processor.h"
#include "common/logging/logger.h"
#include "transport/mux/frame.h"

namespace veil::binding {

namespace {

utils::TokenBucket make_handshake_rate_limiter() {
  return utils::TokenBucket(100.0, std::chrono::milliseconds(1000));
}

constexpr auto kHandshakeClockSkewTolerance = std::chrono::seconds(10);
constexpr std::uint8_t kControlTypeDisconnect = 1;

} // namespace

// ---------------------------------------------------------------------------
// Helpers: config string -> enum
// ---------------------------------------------------------------------------
static obfuscation::ProtocolWrapperType parse_wrapper(const std::string &s) {
  if (s == "websocket")
    return obfuscation::ProtocolWrapperType::kWebSocket;
  if (s == "tls")
    return obfuscation::ProtocolWrapperType::kTLS;
  return obfuscation::ProtocolWrapperType::kNone;
}

static obfuscation::PersonaPreset parse_preset(const std::string &s) {
  if (s == "browser_ws")
    return obfuscation::PersonaPreset::kBrowserWs;
  if (s == "quic_media")
    return obfuscation::PersonaPreset::kQuicMedia;
  if (s == "interactive_game")
    return obfuscation::PersonaPreset::kInteractiveGameUdp;
  if (s == "low_noise_enterprise")
    return obfuscation::PersonaPreset::kLowNoiseEnterprise;
  return obfuscation::PersonaPreset::kCustom;
}

// ---------------------------------------------------------------------------
// VeilNode
// ---------------------------------------------------------------------------
VeilNode::VeilNode(NodeConfig config) : config_(std::move(config)) {}

VeilNode::~VeilNode() { stop(); }

std::int64_t VeilNode::steady_clock_millis() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

void VeilNode::set_callbacks(NodeCallbacks callbacks) {
  callbacks_ = std::move(callbacks);
}

std::string
VeilNode::make_endpoint_key(const transport::UdpEndpoint &endpoint) {
  return endpoint.host + ":" + std::to_string(endpoint.port);
}

void VeilNode::start() {
  if (running_.exchange(true)) {
    LOG_WARN("[VeilNode] start() called while already running");
    return;
  }

  std::error_code ec;
  const std::uint16_t bind_port =
      config_.is_client ? config_.local_port : config_.port;
  if (!socket_.open(bind_port, /*reuse_port=*/true, ec)) {
    running_.store(false);
    throw std::runtime_error("VeilNode: failed to open UDP socket: " +
                             ec.message());
  }

  if (config_.psk.empty()) {
    running_.store(false);
    socket_.close();
    throw std::runtime_error("VeilNode: handshake PSK must not be empty");
  }

  {
    std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
    pending_client_endpoint_.reset();
    pending_client_initiator_.reset();
    if (!config_.is_client) {
      responder_ = std::make_unique<handshake::HandshakeResponder>(
          config_.psk, kHandshakeClockSkewTolerance,
          make_handshake_rate_limiter());
    } else {
      responder_.reset();
    }
  }

  LOG_INFO("[VeilNode] started, bind_port={}, is_client={}", bind_port,
           config_.is_client);

  io_thread_ = std::thread([this]() { io_loop(); });
  timer_thread_ = std::thread([this]() { timer_loop(); });
}

void VeilNode::stop() {
  if (!running_.exchange(false)) {
    return;
  }
  LOG_INFO("[VeilNode] stopping...");

  std::vector<std::shared_ptr<PeerSession>> peers;
  std::vector<std::uint64_t> disconnected_session_ids;
  {
    std::lock_guard<std::mutex> lock(sessions_mutex_);
    peers.reserve(sessions_.size());
    disconnected_session_ids.reserve(sessions_.size());
    for (const auto &[id, peer] : sessions_) {
      peers.push_back(peer);
      disconnected_session_ids.push_back(id);
    }
  }

  for (const auto &peer : peers) {
    send_control_frame_sync(peer, kControlTypeDisconnect);
  }

  socket_.close();

  if (io_thread_.joinable())
    io_thread_.join();
  if (timer_thread_.joinable())
    timer_thread_.join();

  {
    std::lock_guard<std::mutex> lock(sessions_mutex_);
    sessions_.clear();
    endpoint_sessions_.clear();
  }

  {
    std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
    pending_client_endpoint_.reset();
    pending_client_initiator_.reset();
    pending_client_started_at_.reset();
    responder_.reset();
  }

  for (const auto &peer : peers) {
    if (peer && peer->pipeline) {
      peer->pipeline->stop();
    }
  }
  for (const auto session_id : disconnected_session_ids) {
    emit_disconnected(session_id, "node stopped");
  }
  join_teardown_threads();
  LOG_INFO("[VeilNode] stopped");
}

void VeilNode::connect(const std::string &host, std::uint16_t port) {
  if (!config_.is_client) {
    throw std::logic_error("connect() is only valid in client mode");
  }
  if (!running_.load()) {
    throw std::logic_error("connect() requires start() to be called first");
  }

  const transport::UdpEndpoint ep{host, port};
  std::vector<std::uint8_t> init_packet;
  {
    std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
    auto initiator = std::make_unique<handshake::HandshakeInitiator>(
        config_.psk, kHandshakeClockSkewTolerance);
    init_packet = initiator->create_init();
    pending_client_endpoint_ = ep;
    pending_client_initiator_ = std::move(initiator);
    pending_client_started_at_ = std::chrono::steady_clock::now();
  }

  std::error_code ec;
  if (!socket_.send(init_packet, ep, ec)) {
    std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
    pending_client_endpoint_.reset();
    pending_client_initiator_.reset();
    throw std::runtime_error("VeilNode: failed to send handshake init: " +
                             ec.message());
  }
}

bool VeilNode::send(std::uint64_t session_id,
                    std::span<const std::uint8_t> data,
                    std::uint64_t stream_id) {
  std::shared_ptr<PeerSession> peer;
  {
    std::lock_guard<std::mutex> lock(sessions_mutex_);
    peer = find_session(session_id);
  }
  if (!peer) {
    LOG_WARN("[VeilNode] send(): unknown session_id={:#x}", session_id);
    return false;
  }
  const bool queued =
      peer->pipeline->submit_tx(session_id, data, peer->endpoint, stream_id);
  if (queued) {
    mark_session_activity(peer);
  }
  return queued;
}

bool VeilNode::disconnect(std::uint64_t session_id) {
  auto peer = find_session(session_id);
  if (!peer) {
    LOG_WARN("[VeilNode] disconnect(): unknown session_id={:#x}", session_id);
    return false;
  }

  if (!send_control_frame_sync(peer, kControlTypeDisconnect)) {
    LOG_WARN("[VeilNode] disconnect(): failed to send disconnect control frame "
             "session_id={:#x}",
             session_id);
  }

  auto removed_peer = remove_session(session_id);
  if (!removed_peer) {
    return false;
  }
  teardown_session_async(std::move(removed_peer), "disconnect requested", true);
  return true;
}

std::unordered_map<std::string, std::uint64_t> VeilNode::stats() const {
  std::vector<std::shared_ptr<PeerSession>> peers;
  std::uint64_t active_sessions = 0;
  {
    std::lock_guard<std::mutex> lock(sessions_mutex_);
    active_sessions = static_cast<std::uint64_t>(sessions_.size());
    peers.reserve(sessions_.size());
    for (const auto &[id, peer] : sessions_) {
      peers.push_back(peer);
    }
  }

  std::unordered_map<std::string, std::uint64_t> result;
  std::uint64_t rx = 0, tx = 0, rx_bytes = 0, tx_bytes = 0, processed = 0;
  std::uint64_t decrypt_errors = 0, tx_crypto_errors = 0, callback_errors = 0;
  std::uint64_t processing_exceptions = 0, drops = 0;
  std::uint64_t dropped_replay = 0, dropped_decrypt = 0;
  std::uint64_t dropped_prelude = 0, dropped_wrapper = 0, dropped_malformed = 0;
  std::uint64_t dropped_small_packet = 0, dropped_frame_decode = 0;
  std::uint64_t dropped_auth = 0, dropped_fragment_invalid = 0;
  std::uint64_t dropped_fragment_reassembly = 0,
                dropped_fragment_size_mismatch = 0;
  std::uint64_t fragments_sent = 0, fragments_received = 0;
  std::uint64_t messages_reassembled = 0, retransmits = 0,
                session_rotations = 0;
  std::uint64_t ack_sent = 0, ack_coalesced = 0, ack_delayed = 0,
                ack_immediate = 0;
  std::uint64_t ack_gaps_detected = 0;
  std::uint64_t congestion_duplicate_acks = 0, congestion_fast_retransmits = 0;
  std::uint64_t congestion_timeout_retransmits = 0,
                congestion_pacing_delays = 0;
  std::uint64_t congestion_pacing_tokens_granted = 0, congestion_peak_cwnd = 0;
  std::uint64_t reassembly_fast_path_pushes = 0, reassembly_fallback_pushes = 0;
  std::uint64_t reassembly_fallback_transitions = 0;
  std::uint64_t reassembly_fast_path_messages = 0,
                reassembly_fallback_messages = 0;
  std::uint64_t reassembly_fast_path_bytes = 0, reassembly_fallback_bytes = 0;
#if VEIL_ENABLE_TRANSPORT_DIAGNOSTIC_COUNTERS
  const auto &socket_diagnostics = socket_.diagnostic_stats();
#endif
  for (const auto &peer : peers) {
    const auto &s = peer->pipeline->stats();
    rx += s.rx_packets.load();
    tx += s.tx_packets.load();
    rx_bytes += s.rx_bytes.load();
    tx_bytes += s.tx_bytes.load();
    processed += s.processed_packets.load();
    decrypt_errors += s.decrypt_errors.load();
    tx_crypto_errors += s.tx_crypto_errors.load();
    callback_errors += s.callback_errors.load();
    processing_exceptions += s.processing_exceptions.load();
    drops += s.queue_full_drops.load();
    const auto transport_stats = peer->pipeline->execute_on_session(
        [](transport::TransportSession &session) { return session.stats(); });
    dropped_replay += transport_stats.packets_dropped_replay;
    dropped_decrypt += transport_stats.packets_dropped_decrypt;
    dropped_prelude += transport_stats.packets_dropped_prelude;
    dropped_wrapper += transport_stats.packets_dropped_wrapper;
    dropped_malformed += transport_stats.packets_dropped_malformed;
    dropped_small_packet += transport_stats.packets_dropped_small_packet;
    dropped_frame_decode += transport_stats.packets_dropped_frame_decode;
    dropped_auth += transport_stats.packets_dropped_auth;
    dropped_fragment_invalid +=
        transport_stats.packets_dropped_fragment_invalid;
    dropped_fragment_reassembly +=
        transport_stats.packets_dropped_fragment_reassembly;
    dropped_fragment_size_mismatch +=
        transport_stats.packets_dropped_fragment_size_mismatch;
    fragments_sent += transport_stats.fragments_sent;
    fragments_received += transport_stats.fragments_received;
    messages_reassembled += transport_stats.messages_reassembled;
    retransmits += transport_stats.retransmits;
    session_rotations += transport_stats.session_rotations;
    const auto ack_stats = peer->pipeline->execute_on_session(
        [](transport::TransportSession &session) {
          return session.ack_scheduler_stats();
        });
    ack_sent += ack_stats.acks_sent;
    ack_coalesced += ack_stats.acks_coalesced;
    ack_delayed += ack_stats.acks_delayed;
    ack_immediate += ack_stats.acks_immediate;
    ack_gaps_detected += ack_stats.gaps_detected;
    const auto congestion_stats = peer->pipeline->execute_on_session(
        [](transport::TransportSession &session) {
          return session.congestion_stats();
        });
    congestion_duplicate_acks += congestion_stats.duplicate_acks;
    congestion_fast_retransmits += congestion_stats.fast_retransmits;
    congestion_timeout_retransmits += congestion_stats.timeout_retransmits;
    congestion_pacing_delays += congestion_stats.pacing_delays;
    congestion_pacing_tokens_granted += congestion_stats.pacing_tokens_granted;
    congestion_peak_cwnd +=
        static_cast<std::uint64_t>(congestion_stats.peak_cwnd);
#if VEIL_ENABLE_TRANSPORT_DIAGNOSTIC_COUNTERS
    const auto reassembly_stats = peer->pipeline->execute_on_session(
        [](transport::TransportSession &session) {
          return session.fragment_reassembly_diagnostic_stats();
        });
    reassembly_fast_path_pushes += reassembly_stats.fast_path_pushes;
    reassembly_fallback_pushes += reassembly_stats.fallback_pushes;
    reassembly_fallback_transitions += reassembly_stats.fallback_transitions;
    reassembly_fast_path_messages +=
        reassembly_stats.fast_path_messages_reassembled;
    reassembly_fallback_messages +=
        reassembly_stats.fallback_messages_reassembled;
    reassembly_fast_path_bytes += reassembly_stats.fast_path_bytes_reassembled;
    reassembly_fallback_bytes += reassembly_stats.fallback_bytes_reassembled;
#endif
  }
  result["rx_packets"] = rx;
  result["tx_packets"] = tx;
  result["rx_bytes"] = rx_bytes;
  result["tx_bytes"] = tx_bytes;
  result["processed_packets"] = processed;
  result["decrypt_errors"] = decrypt_errors;
  result["tx_crypto_errors"] = tx_crypto_errors;
  result["callback_errors"] = callback_errors;
  result["processing_exceptions"] = processing_exceptions;
  result["queue_full_drops"] = drops;
  result["transport_packets_dropped_replay"] = dropped_replay;
  result["transport_packets_dropped_decrypt"] = dropped_decrypt;
  result["transport_packets_dropped_prelude"] = dropped_prelude;
  result["transport_packets_dropped_wrapper"] = dropped_wrapper;
  result["transport_packets_dropped_malformed"] = dropped_malformed;
  result["transport_packets_dropped_small_packet"] = dropped_small_packet;
  result["transport_packets_dropped_frame_decode"] = dropped_frame_decode;
  result["transport_packets_dropped_auth"] = dropped_auth;
  result["transport_packets_dropped_fragment_invalid"] =
      dropped_fragment_invalid;
  result["transport_packets_dropped_fragment_reassembly"] =
      dropped_fragment_reassembly;
  result["transport_packets_dropped_fragment_size_mismatch"] =
      dropped_fragment_size_mismatch;
  result["transport_fragments_sent"] = fragments_sent;
  result["transport_fragments_received"] = fragments_received;
  result["transport_messages_reassembled"] = messages_reassembled;
  result["transport_retransmits"] = retransmits;
  result["transport_session_rotations"] = session_rotations;
  result["transport_ack_sent"] = ack_sent;
  result["transport_ack_coalesced"] = ack_coalesced;
  result["transport_ack_delayed"] = ack_delayed;
  result["transport_ack_immediate"] = ack_immediate;
  result["transport_ack_gaps_detected"] = ack_gaps_detected;
  result["transport_congestion_duplicate_acks"] = congestion_duplicate_acks;
  result["transport_congestion_fast_retransmits"] = congestion_fast_retransmits;
  result["transport_congestion_timeout_retransmits"] =
      congestion_timeout_retransmits;
  result["transport_congestion_pacing_delays"] = congestion_pacing_delays;
  result["transport_congestion_pacing_tokens_granted"] =
      congestion_pacing_tokens_granted;
  result["transport_congestion_peak_cwnd"] = congestion_peak_cwnd;
  result["transport_reassembly_fast_path_pushes"] = reassembly_fast_path_pushes;
  result["transport_reassembly_fallback_pushes"] = reassembly_fallback_pushes;
  result["transport_reassembly_fallback_transitions"] =
      reassembly_fallback_transitions;
  result["transport_reassembly_fast_path_messages"] =
      reassembly_fast_path_messages;
  result["transport_reassembly_fallback_messages"] =
      reassembly_fallback_messages;
  result["transport_reassembly_fast_path_bytes"] = reassembly_fast_path_bytes;
  result["transport_reassembly_fallback_bytes"] = reassembly_fallback_bytes;
#if VEIL_ENABLE_TRANSPORT_DIAGNOSTIC_COUNTERS
  result["udp_tx_send_batch_calls"] = socket_diagnostics.tx_send_batch_calls;
  result["udp_tx_packets_via_sendmmsg"] =
      socket_diagnostics.tx_packets_via_sendmmsg;
  result["udp_tx_packets_via_sendto_fallback"] =
      socket_diagnostics.tx_packets_via_sendto_fallback;
  result["udp_tx_sendto_calls"] = socket_diagnostics.tx_sendto_calls;
  result["udp_rx_poll_wakeups"] = socket_diagnostics.rx_poll_wakeups;
  result["udp_rx_recvmmsg_calls"] = socket_diagnostics.rx_recvmmsg_calls;
  result["udp_rx_packets_via_recvmmsg"] =
      socket_diagnostics.rx_packets_via_recvmmsg;
  result["udp_rx_recvfrom_calls"] = socket_diagnostics.rx_recvfrom_calls;
  result["udp_rx_recvfrom_fallback_calls"] =
      socket_diagnostics.rx_recvfrom_fallback_calls;
  result["udp_rx_packets_delivered"] = socket_diagnostics.rx_packets_delivered;
#endif
  result["active_sessions"] = active_sessions;
  return result;
}

// ---------------------------------------------------------------------------
// Private
// ---------------------------------------------------------------------------
void VeilNode::io_loop() {
  LOG_DEBUG("[VeilNode] IO thread started");
  std::error_code ec;

  while (running_.load()) {
    // Poll blocks for up to 50 ms so that stop() is noticed promptly.
    socket_.poll(
        [this](const transport::UdpPacket &pkt) { dispatch_packet(pkt); },
        /*timeout_ms=*/50, ec);

    if (ec) {
      LOG_WARN("[VeilNode] poll error: {}", ec.message());
      ec.clear();
    }
  }
  LOG_DEBUG("[VeilNode] IO thread exiting");
}

void VeilNode::timer_loop() {
  LOG_DEBUG("[VeilNode] Timer thread started");
  using namespace std::chrono_literals;

  while (running_.load()) {
    std::this_thread::sleep_for(100ms);

    bool handshake_timed_out = false;
    {
      std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
      if (pending_client_endpoint_ && pending_client_initiator_ &&
          pending_client_started_at_) {
        const auto elapsed =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - *pending_client_started_at_);
        if (elapsed >=
            std::chrono::milliseconds(config_.handshake_timeout_ms)) {
          pending_client_endpoint_.reset();
          pending_client_initiator_.reset();
          pending_client_started_at_.reset();
          handshake_timed_out = true;
        }
      }
    }
    if (handshake_timed_out) {
      emit_error(0, "Handshake timed out");
    }

    std::vector<std::pair<std::uint64_t, std::shared_ptr<PeerSession>>> peers;
    {
      std::lock_guard<std::mutex> lock(sessions_mutex_);
      peers.reserve(sessions_.size());
      for (const auto &[id, peer] : sessions_) {
        peers.emplace_back(id, peer);
      }
    }

    for (const auto &[id, peer] : peers) {
      if (config_.session_idle_timeout_ms > 0) {
        const auto idle_for_ms =
            steady_clock_millis() - peer->last_activity_ms.load();
        if (idle_for_ms >= config_.session_idle_timeout_ms) {
          send_control_frame_sync(peer, kControlTypeDisconnect);
          auto removed_peer = remove_session(id);
          if (removed_peer) {
            teardown_session_async(std::move(removed_peer),
                                   "session idle timeout", true);
          }
          continue;
        }
      }

      // Check session rotation via the safe execute_on_session wrapper.
      bool should_rotate = peer->pipeline->execute_on_session(
          [](transport::TransportSession &s) {
            return s.should_rotate_session();
          });

      if (should_rotate) {
        peer->pipeline->execute_on_session(
            [](transport::TransportSession &s) { s.rotate_session(); });
        LOG_DEBUG("[VeilNode] Rotated session_id={:#x}", id);
      }
    }
  }
  LOG_DEBUG("[VeilNode] Timer thread exiting");
}

void VeilNode::dispatch_packet(const transport::UdpPacket &packet) {
  const std::string ep_key = make_endpoint_key(packet.remote);

  std::shared_ptr<PeerSession> peer;
  bool is_new_session = false;
  bool should_submit_to_pipeline = false;
  std::uint64_t session_id = 0;
  std::string new_host;
  std::uint16_t new_port = 0;
  std::optional<std::vector<std::uint8_t>> handshake_response;
  std::optional<std::string> handshake_error;

  {
    std::lock_guard<std::mutex> lock(sessions_mutex_);
    peer = find_session_by_endpoint_key(ep_key);
  }
  should_submit_to_pipeline = peer != nullptr;

  if (!peer && !config_.is_client) {
    std::optional<handshake::HandshakeResponder::Result> handshake_result;
    {
      std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
      if (responder_) {
        handshake_result = responder_->handle_init(packet.data, ep_key);
      }
    }

    if (handshake_result) {
      std::lock_guard<std::mutex> lock(sessions_mutex_);
      peer = create_session(handshake_result->session, packet.remote);
      handshake_response = std::move(handshake_result->response);
      session_id = peer->session_id;
      is_new_session = true;
      should_submit_to_pipeline = false;
      new_host = packet.remote.host;
      new_port = packet.remote.port;
    }
  } else if (!peer && config_.is_client) {
    std::optional<handshake::HandshakeSession> handshake_session;
    {
      std::lock_guard<std::mutex> handshake_lock(handshake_mutex_);
      if (pending_client_endpoint_ && pending_client_initiator_ &&
          make_endpoint_key(*pending_client_endpoint_) == ep_key) {
        handshake_session =
            pending_client_initiator_->consume_response(packet.data);
        if (handshake_session) {
          pending_client_endpoint_.reset();
          pending_client_initiator_.reset();
          pending_client_started_at_.reset();
        } else {
          pending_client_endpoint_.reset();
          pending_client_initiator_.reset();
          pending_client_started_at_.reset();
          handshake_error = "Handshake response validation failed";
        }
      }
    }

    if (handshake_session) {
      std::lock_guard<std::mutex> lock(sessions_mutex_);
      peer = create_session(*handshake_session, packet.remote);
      session_id = peer->session_id;
      is_new_session = true;
      should_submit_to_pipeline = false;
      new_host = packet.remote.host;
      new_port = packet.remote.port;
    }
  }

  if (handshake_response) {
    std::error_code ec;
    if (!socket_.send(*handshake_response, packet.remote, ec)) {
      emit_error(session_id, "Handshake response send failed: " + ec.message());
    }
  }

  if (handshake_error) {
    emit_error(0, *handshake_error);
  }

  // Fire callback OUTSIDE the lock to prevent deadlock (Python code
  // may call back into send() which also takes sessions_mutex_).
  if (is_new_session && callbacks_.on_new_connection) {
    callbacks_.on_new_connection(peer->session_id, new_host, new_port);
  }

  if (!peer) {
    LOG_DEBUG("[VeilNode] Dropping packet from unknown peer {}:{}",
              packet.remote.host, packet.remote.port);
    return;
  }

  if (should_submit_to_pipeline) {
    mark_session_activity(peer);
    peer->pipeline->submit_rx(peer->session_id, packet.data, packet.remote);
  }
}

std::shared_ptr<PeerSession>
VeilNode::create_session(const handshake::HandshakeSession &handshake_session,
                         const transport::UdpEndpoint &endpoint) {
  transport::TransportSessionConfig cfg;
  cfg.mtu = config_.mtu;
  cfg.protocol_wrapper = parse_wrapper(config_.protocol_wrapper);
  cfg.enable_http_handshake_emulation = config_.enable_http_handshake_emulation;
  cfg.session_rotation_interval =
      std::chrono::seconds(config_.rotation_interval_seconds);
  const auto preset = parse_preset(config_.persona_preset);
  if (preset != obfuscation::PersonaPreset::kCustom) {
    cfg.enable_profile_driven_morphing = true;
    cfg.obfuscation_profile.persona_preset = preset;
  }

  auto session =
      std::make_unique<transport::TransportSession>(handshake_session, cfg);

  transport::PipelineConfig pcfg;
  auto pipeline =
      std::make_unique<transport::PipelineProcessor>(session.get(), pcfg);

  const std::uint64_t sid = handshake_session.session_id;
  pipeline->set_socket(&socket_);
  pipeline->start(
      /*on_rx=*/[this, sid](std::uint64_t /*id*/,
                            const std::vector<mux::MuxFrame> &frames,
                            const transport::UdpEndpoint
                                &src) { emit_data(sid, frames, src); },
      /*on_tx_complete=*/nullptr,
      /*on_error=*/
      [this, sid](std::uint64_t /*id*/, const std::string &msg) {
        emit_error(sid, msg);
      });

  auto peer = std::make_shared<PeerSession>();
  peer->session = std::move(session);
  peer->pipeline = std::move(pipeline);
  peer->endpoint = endpoint;
  peer->session_id = sid;
  peer->last_activity_ms.store(steady_clock_millis());

  auto [it, _] = sessions_.emplace(sid, peer);
  endpoint_sessions_[make_endpoint_key(endpoint)] = sid;
  LOG_INFO("[VeilNode] Created session session_id={:#x}, peer={}:{}", sid,
           endpoint.host, endpoint.port);
  return it->second;
}

std::shared_ptr<PeerSession>
VeilNode::find_session(std::uint64_t session_id) const {
  auto it = sessions_.find(session_id);
  return it != sessions_.end() ? it->second : nullptr;
}

std::shared_ptr<PeerSession>
VeilNode::find_session_by_endpoint_key(const std::string &endpoint_key) const {
  auto it = endpoint_sessions_.find(endpoint_key);
  if (it == endpoint_sessions_.end()) {
    return nullptr;
  }
  return find_session(it->second);
}

std::shared_ptr<PeerSession>
VeilNode::remove_session(std::uint64_t session_id) {
  std::lock_guard<std::mutex> lock(sessions_mutex_);
  auto it = sessions_.find(session_id);
  if (it == sessions_.end()) {
    return nullptr;
  }

  auto peer = it->second;
  const auto endpoint_key = make_endpoint_key(peer->endpoint);
  auto endpoint_it = endpoint_sessions_.find(endpoint_key);
  if (endpoint_it != endpoint_sessions_.end() &&
      endpoint_it->second == session_id) {
    endpoint_sessions_.erase(endpoint_it);
  }
  sessions_.erase(it);
  return peer;
}

void VeilNode::teardown_session_async(std::shared_ptr<PeerSession> peer,
                                      std::string reason,
                                      bool emit_disconnect) {
  const auto session_id = peer ? peer->session_id : 0;
  std::lock_guard<std::mutex> lock(teardown_threads_mutex_);
  teardown_threads_.emplace_back([this, peer = std::move(peer),
                                  reason = std::move(reason), emit_disconnect,
                                  session_id]() mutable {
    if (peer && peer->pipeline) {
      peer->pipeline->stop();
    }
    if (emit_disconnect) {
      emit_disconnected(session_id, reason);
    }
  });
}

bool VeilNode::handle_control_frame(std::uint64_t session_id,
                                    const mux::ControlFrame &frame) {
  if (frame.type != kControlTypeDisconnect) {
    return false;
  }

  auto peer = remove_session(session_id);
  if (!peer) {
    return true;
  }

  teardown_session_async(std::move(peer), "peer disconnected", true);
  return true;
}

bool VeilNode::send_control_frame_sync(const std::shared_ptr<PeerSession> &peer,
                                       std::uint8_t type,
                                       std::span<const std::uint8_t> payload) {
  if (!peer || !peer->pipeline) {
    return false;
  }

  std::vector<std::uint8_t> payload_copy(payload.begin(), payload.end());
  auto encrypted = peer->pipeline->execute_on_session(
      [&](transport::TransportSession &session) {
        return session.encrypt_frame(
            mux::make_control_frame(type, std::move(payload_copy)));
      });
  if (!encrypted) {
    return false;
  }

  std::error_code ec;
  return socket_.send(*encrypted, peer->endpoint, ec);
}

void VeilNode::mark_session_activity(const std::shared_ptr<PeerSession> &peer) {
  if (!peer) {
    return;
  }
  peer->last_activity_ms.store(steady_clock_millis());
}

void VeilNode::join_teardown_threads() {
  std::vector<std::thread> threads;
  {
    std::lock_guard<std::mutex> lock(teardown_threads_mutex_);
    threads.swap(teardown_threads_);
  }

  for (auto &thread : threads) {
    if (thread.joinable()) {
      thread.join();
    }
  }
}

void VeilNode::emit_data(std::uint64_t session_id,
                         const std::vector<mux::MuxFrame> &frames,
                         const transport::UdpEndpoint & /*source*/) {
  for (const auto &frame : frames) {
    if (frame.kind == mux::FrameKind::kControl) {
      if (handle_control_frame(session_id, frame.control)) {
        return;
      }
      continue;
    }
    if (!callbacks_.on_data) {
      continue;
    }
    if (frame.kind == mux::FrameKind::kData) {
      callbacks_.on_data(session_id, frame.data.stream_id, frame.data.payload);
    }
  }
}

void VeilNode::emit_error(std::uint64_t session_id,
                          const std::string &message) {
  if (callbacks_.on_error) {
    callbacks_.on_error(session_id, message);
  }
}

void VeilNode::emit_disconnected(std::uint64_t session_id,
                                 const std::string &reason) {
  if (callbacks_.on_disconnected) {
    callbacks_.on_disconnected(session_id, reason);
  }
}

} // namespace veil::binding
