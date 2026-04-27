// ============================================================
// FILE: front_end/webrtc_config.dart
// ============================================================
//
// Centralized WebRTC ICE server configuration.
//
// Intranet deployment (default)
// ──────────────────────────────
// On a local LAN every device has a routable host IP, so WebRTC
// can connect via *host ICE candidates* — direct LAN addresses —
// without any STUN or TURN server.  Leaving iceServers empty is
// the correct setting for intranet-only deployments and removes
// the runtime dependency on stun.l.google.com.
//
// If you ever need to support clients outside the LAN (VPN users,
// remote admin access) add a local STUN/TURN server entry:
//
//   static const iceConfig = {
//     'iceServers': [
//       {'urls': 'stun:192.168.1.100:3478'},          // coturn on LAN
//       {
//         'urls':       'turn:192.168.1.100:3478',
//         'username':   'phonebox',
//         'credential': 'changeme',
//       },
//     ],
//   };
//
// coturn install:  sudo apt install coturn
// ============================================================

class WebRTCConfig {
  WebRTCConfig._();

  /// ICE configuration passed to every `createPeerConnection()` call.
  ///
  /// Empty iceServers = host candidates only (LAN direct).
  /// Works perfectly when server and clients share the same subnet.
  static const Map<String, dynamic> iceConfig = {
    'iceServers': <Map<String, dynamic>>[],
  };

  /// Offer constraints used for all video-only streams.
  static const Map<String, dynamic> videoOfferConstraints = {
    'offerToReceiveVideo': true,
    'offerToReceiveAudio': false,
  };
}