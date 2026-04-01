/**
 * pybind11 bindings for the Veil Core protocol library.
 *
 * Exposes:
 *   _veil_core_ext.NodeConfig   — configuration dataclass
 *   _veil_core_ext.VeilNode     — UDP node (server or client)
 *
 * Callbacks from C++ worker threads acquire the GIL via
 * py::gil_scoped_acquire before touching any Python object.
 */

#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <mutex>
#include <unordered_map>

#include "node.h"

namespace py = pybind11;
using namespace veil::binding;

/**
 * Helper: stores all four Python callables and produces a NodeCallbacks
 * struct with GIL-acquiring trampolines. This avoids the previous bug
 * where each property setter overwrote ALL callbacks with a new struct.
 */
struct CallbackHolder {
  py::object py_on_new_connection = py::none();
  py::object py_on_data = py::none();
  py::object py_on_disconnected = py::none();
  py::object py_on_error = py::none();

  NodeCallbacks build() const {
    NodeCallbacks cbs{};

    if (!py_on_new_connection.is_none()) {
      auto shared = std::make_shared<py::object>(py_on_new_connection);
      cbs.on_new_connection = [shared](std::uint64_t sid,
                                       const std::string &host,
                                       std::uint16_t port) {
        py::gil_scoped_acquire gil;
        (*shared)(sid, host, static_cast<int>(port));
      };
    }

    if (!py_on_data.is_none()) {
      auto shared = std::make_shared<py::object>(py_on_data);
      cbs.on_data = [shared](std::uint64_t sid, std::uint64_t stream_id,
                             std::vector<std::uint8_t> data) {
        py::gil_scoped_acquire gil;
        (*shared)(sid, stream_id,
                  py::bytes(reinterpret_cast<const char *>(data.data()),
                            data.size()));
      };
    }

    if (!py_on_disconnected.is_none()) {
      auto shared = std::make_shared<py::object>(py_on_disconnected);
      cbs.on_disconnected = [shared](std::uint64_t sid,
                                     const std::string &reason) {
        py::gil_scoped_acquire gil;
        (*shared)(sid, reason);
      };
    }

    if (!py_on_error.is_none()) {
      auto shared = std::make_shared<py::object>(py_on_error);
      cbs.on_error = [shared](std::uint64_t sid, const std::string &msg) {
        py::gil_scoped_acquire gil;
        (*shared)(sid, msg);
      };
    }

    return cbs;
  }
};

// Per-node callback state. Stored externally since VeilNode is a C++ class
// and doesn't know about py::object.
static std::unordered_map<VeilNode *, CallbackHolder> g_holders;
static std::mutex g_holders_mutex;

template <typename UpdateFn>
static void update_callbacks(VeilNode &node, UpdateFn &&update_fn) {
  NodeCallbacks callbacks;
  {
    std::lock_guard<std::mutex> lock(g_holders_mutex);
    auto &holder = g_holders[&node];
    update_fn(holder);
    callbacks = holder.build();
  }
  node.set_callbacks(std::move(callbacks));
}

PYBIND11_MODULE(_veil_core_ext, m) {
  m.doc() = "Veil Protocol C++ core — Python extension";

  py::class_<ClientCredential>(m, "ClientCredential")
      .def(py::init<>())
      .def_readwrite("client_id", &ClientCredential::client_id)
      .def_readwrite("enabled", &ClientCredential::enabled)
      .def_property(
          "psk",
          [](const ClientCredential &c) {
            return py::bytes(reinterpret_cast<const char *>(c.psk.data()),
                             c.psk.size());
          },
          [](ClientCredential &c, py::bytes value) {
            std::string raw = value;
            c.psk.assign(raw.begin(), raw.end());
          });

  // -----------------------------------------------------------------------
  // NodeConfig
  // -----------------------------------------------------------------------
  py::class_<NodeConfig>(m, "NodeConfig")
      .def(py::init<>())
      .def_readwrite("host", &NodeConfig::host)
      .def_readwrite("port", &NodeConfig::port)
      .def_readwrite("local_port", &NodeConfig::local_port)
      .def_readwrite("is_client", &NodeConfig::is_client)
      .def_readwrite("protocol_wrapper", &NodeConfig::protocol_wrapper)
      .def_readwrite("persona_preset", &NodeConfig::persona_preset)
      .def_readwrite("enable_http_handshake_emulation",
                     &NodeConfig::enable_http_handshake_emulation)
      .def_readwrite("rotation_interval_seconds",
                     &NodeConfig::rotation_interval_seconds)
      .def_readwrite("handshake_timeout_ms", &NodeConfig::handshake_timeout_ms)
      .def_readwrite("session_idle_timeout_ms",
                     &NodeConfig::session_idle_timeout_ms)
      .def_readwrite("mtu", &NodeConfig::mtu)
      .def_readwrite("client_id", &NodeConfig::client_id)
      .def_readwrite("clients", &NodeConfig::clients)
      .def_property(
          "psk",
          [](const NodeConfig &c) {
            return py::bytes(reinterpret_cast<const char *>(c.psk.data()),
                             c.psk.size());
          },
          [](NodeConfig &c, py::bytes value) {
            std::string raw = value;
            c.psk.assign(raw.begin(), raw.end());
          })
      .def_property(
          "fallback_psk",
          [](const NodeConfig &c) {
            return py::bytes(reinterpret_cast<const char *>(c.fallback_psk.data()),
                             c.fallback_psk.size());
          },
          [](NodeConfig &c, py::bytes value) {
            std::string raw = value;
            c.fallback_psk.assign(raw.begin(), raw.end());
          })
      .def_readwrite("fallback_psk_policy", &NodeConfig::fallback_psk_policy)
      .def_readwrite("allow_legacy_unhinted",
                     &NodeConfig::allow_legacy_unhinted)
      .def_readwrite("allow_hinted_route_miss_global_fallback",
                     &NodeConfig::allow_hinted_route_miss_global_fallback)
      .def_readwrite("max_legacy_trial_decrypt_attempts",
                     &NodeConfig::max_legacy_trial_decrypt_attempts)
      .def("__repr__", [](const NodeConfig &c) {
        return "<NodeConfig host=" + c.host +
               " port=" + std::to_string(c.port) +
               " wrapper=" + c.protocol_wrapper + ">";
      });

  // -----------------------------------------------------------------------
  // VeilNode
  // -----------------------------------------------------------------------
  py::class_<VeilNode>(m, "VeilNode")
      .def(py::init<NodeConfig>(), py::arg("config"))

      // Callback setters — each setter updates a single slot in the
      // CallbackHolder, then rebuilds the full NodeCallbacks struct
      // so that previously-set callbacks are preserved.
      .def_property("on_new_connection", nullptr,
                    [](VeilNode &node, py::object cb) {
                      update_callbacks(node, [&cb](CallbackHolder &holder) {
                        holder.py_on_new_connection = cb;
                      });
                    })

      .def_property("on_data", nullptr,
                    [](VeilNode &node, py::object cb) {
                      update_callbacks(node, [&cb](CallbackHolder &holder) {
                        holder.py_on_data = cb;
                      });
                    })

      .def_property("on_disconnected", nullptr,
                    [](VeilNode &node, py::object cb) {
                      update_callbacks(node, [&cb](CallbackHolder &holder) {
                        holder.py_on_disconnected = cb;
                      });
                    })

      .def_property("on_error", nullptr,
                    [](VeilNode &node, py::object cb) {
                      update_callbacks(node, [&cb](CallbackHolder &holder) {
                        holder.py_on_error = cb;
                      });
                    })

      .def("start", &VeilNode::start,
           "Bind the UDP socket and start the C++ worker threads.")

      .def(
          "stop",
          [](VeilNode &node) {
            node.stop();
            // Clean up the holder to release py::object refs.
            std::lock_guard<std::mutex> lock(g_holders_mutex);
            g_holders.erase(&node);
          },
          "Gracefully stop all threads and close the socket.",
          py::call_guard<py::gil_scoped_release>())

      .def("connect", &VeilNode::connect, py::arg("host"), py::arg("port"),
           "(Client mode) Initiate connection to remote host:port.")

      .def(
          "send",
          [](VeilNode &node, std::uint64_t session_id, py::bytes data,
             std::uint64_t stream_id) -> bool {
            std::string_view sv = data;
            const auto *ptr = reinterpret_cast<const std::uint8_t *>(sv.data());
            return node.send(session_id,
                             std::span<const std::uint8_t>(ptr, sv.size()),
                             stream_id);
          },
          py::arg("session_id"), py::arg("data"), py::arg("stream_id") = 1,
          "Encrypt and enqueue *data* for delivery to *session_id*.\n"
          "Returns True if queued, False on back-pressure (queue full).")

      .def("disconnect", &VeilNode::disconnect, py::arg("session_id"),
           "Drop a local session and fire on_disconnected if it existed.")

      .def("stats", &VeilNode::stats,
           "Return a dict of aggregate pipeline statistics.")

      .def("__repr__", [](const VeilNode &) { return "<VeilNode>"; })

      // Release callback references when the Python object is
      // garbage-collected.
      .def("__del__", [](VeilNode &node) {
        std::lock_guard<std::mutex> lock(g_holders_mutex);
        g_holders.erase(&node);
      });
}
