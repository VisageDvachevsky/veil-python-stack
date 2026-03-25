#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#include "common/handshake/handshake_processor.h"
#include "common/logging/logger.h"
#include "common/utils/rate_limiter.h"
#include "transport/pipeline/pipeline_processor.h"
#include "transport/session/transport_session.h"
#include "transport/udp_socket/udp_socket.h"

namespace veil::binding {

/**
 * Configuration for a VeilNode (server or client).
 * Exposed directly to Python via pybind11.
 */
struct NodeConfig {
  std::string host{"0.0.0.0"};
  std::uint16_t port{4433};
  std::uint16_t local_port{0}; // 0 = OS picks (client mode)
  bool is_client{false};

  // Obfuscation
  std::string protocol_wrapper{"none"}; // "none" | "websocket" | "tls"
  std::string persona_preset{"custom"}; // "custom" | "browser_ws" | ...
  bool enable_http_handshake_emulation{false};

  // Session
  int rotation_interval_seconds{30};
  int handshake_timeout_ms{5000};
  int session_idle_timeout_ms{0};
  std::size_t mtu{1400};
  std::vector<std::uint8_t> psk{32, 0xAB};
};

/**
 * Python-friendly callback descriptors.
 * All callbacks are invoked from C++ worker threads and must therefore be
 * dispatched onto the Python GIL before touching Python objects.
 * The pybind11 binding handles that via py::gil_scoped_acquire in each
 * trampoline.
 */
struct NodeCallbacks {
  // (session_id, remote_host, remote_port)
  std::function<void(std::uint64_t, const std::string &, std::uint16_t)>
      on_new_connection;
  // (session_id, stream_id, data)
  std::function<void(std::uint64_t, std::uint64_t, std::vector<std::uint8_t>)>
      on_data;
  // (session_id, reason)
  std::function<void(std::uint64_t, const std::string &)> on_disconnected;
  // (session_id, error_message)
  std::function<void(std::uint64_t, const std::string &)> on_error;
};

/**
 * Per-peer session state tracked by VeilNode.
 */
struct PeerSession {
  std::unique_ptr<transport::TransportSession> session;
  std::unique_ptr<transport::PipelineProcessor> pipeline;
  transport::UdpEndpoint endpoint;
  std::uint64_t session_id{0};
  std::atomic<std::int64_t> last_activity_ms{0};
  std::atomic<bool> ready_notified{false};
};

/**
 * VeilNode — single UDP node owning all active sessions.
 *
 * In server mode:  binds a fixed port, accepts incoming handshakes.
 * In client mode:  connects to a remote host:port, initiates the handshake.
 *
 * The I/O loop runs on a dedicated thread (io_thread_).
 * Each session has its own PipelineProcessor with two worker threads.
 *
 * This class is exposed to Python via pybind11 (see node_bindings.cpp).
 * All public callbacks are invoked from worker threads and post Python
 * callables back through the GIL.
 */
class VeilNode {
public:
  explicit VeilNode(NodeConfig config);
  ~VeilNode();

  // Non-copyable
  VeilNode(const VeilNode &) = delete;
  VeilNode &operator=(const VeilNode &) = delete;

  /**
   * Register callback functors (called from Python before start()).
   */
  void set_callbacks(NodeCallbacks callbacks);

  /**
   * Bind the socket and start the I/O thread.
   */
  void start();

  /**
   * Gracefully stop all threads and close the socket.
   */
  void stop();

  /**
   * (Client mode) Initiate connection to a remote server.
   * Triggers the handshake; on_new_connection fires when the session is ready.
   */
  void connect(const std::string &host, std::uint16_t port);

  /**
   * Send plaintext data to a specific session.
   * @return true if the data was accepted into the pipeline queue.
   */
  bool send(std::uint64_t session_id, std::span<const std::uint8_t> data,
            std::uint64_t stream_id = 1);

  /**
   * Drop a local session and stop routing packets for it.
   * Fires
   * on_disconnected if the session existed.
   */
  bool disconnect(std::uint64_t session_id);

  int socket_fd() const { return socket_.fd(); }

  /**
   * Collect current pipeline statistics across all active sessions.
   * Returns a map of stat-name -> aggregate count.
   */
  std::unordered_map<std::string, std::uint64_t> stats() const;

private:
  // I/O thread entry-point: polls the UDP socket and dispatches packets.
  void io_loop();

  // Timer thread: checks rotation / heartbeat deadlines periodically.
  void timer_loop();

  // Route an incoming raw UDP packet to the correct session.
  void dispatch_packet(const transport::UdpPacket &packet);

  // Build a new TransportSession + PipelineProcessor for a given handshake.
  std::shared_ptr<PeerSession>
  create_session(const handshake::HandshakeSession &handshake_session,
                 const transport::UdpEndpoint &endpoint);

  // Locate an existing session by ID (nullptr if not found).
  std::shared_ptr<PeerSession> find_session(std::uint64_t session_id) const;

  // Locate an existing session by remote endpoint key (nullptr if not found).
  std::shared_ptr<PeerSession>
  find_session_by_endpoint_key(const std::string &endpoint_key) const;

  static std::string make_endpoint_key(const transport::UdpEndpoint &endpoint);

  std::shared_ptr<PeerSession> remove_session(std::uint64_t session_id);
  void teardown_session_async(std::shared_ptr<PeerSession> peer,
                              std::string reason, bool emit_disconnect);
  bool handle_control_frame(std::uint64_t session_id,
                            const mux::ControlFrame &frame);
  bool send_control_frame_sync(const std::shared_ptr<PeerSession> &peer,
                               std::uint8_t type,
                               std::span<const std::uint8_t> payload = {});
  void maybe_drive_http_prelude(const std::shared_ptr<PeerSession> &peer);
  void maybe_emit_ready(const std::shared_ptr<PeerSession> &peer);
  void mark_session_activity(const std::shared_ptr<PeerSession> &peer);
  static std::int64_t steady_clock_millis();
  void join_teardown_threads();

  // Fire on_data callback safely.
  void emit_data(std::uint64_t session_id,
                 const std::vector<mux::MuxFrame> &frames,
                 const transport::UdpEndpoint &source);

  // Fire on_error callback safely.
  void emit_error(std::uint64_t session_id, const std::string &message);

  // Fire on_disconnected callback safely.
  void emit_disconnected(std::uint64_t session_id, const std::string &reason);

  NodeConfig config_;
  NodeCallbacks callbacks_;

  transport::UdpSocket socket_;
  std::unordered_map<std::uint64_t, std::shared_ptr<PeerSession>> sessions_;
  std::unordered_map<std::string, std::uint64_t> endpoint_sessions_;
  mutable std::mutex sessions_mutex_;
  std::mutex handshake_mutex_;
  std::unique_ptr<handshake::HandshakeResponder> responder_;
  std::optional<transport::UdpEndpoint> pending_client_endpoint_;
  std::unique_ptr<handshake::HandshakeInitiator> pending_client_initiator_;
  std::optional<std::chrono::steady_clock::time_point>
      pending_client_started_at_;

  std::thread io_thread_;
  std::thread timer_thread_;
  std::mutex teardown_threads_mutex_;
  std::vector<std::thread> teardown_threads_;
  std::atomic<bool> running_{false};
};

} // namespace veil::binding
