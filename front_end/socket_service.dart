import 'package:socket_io_client/socket_io_client.dart' as IO;

class SocketService {
  static final SocketService _instance = SocketService._internal();
  factory SocketService() => _instance;
  SocketService._internal();

  late IO.Socket socket;
  bool isConnected = false;

  void connect(Function(dynamic) onScanStatus) {
    socket = IO.io(
      "http://localhost:5000", // Use 127.0.0.1 for Flutter Web
      <String, dynamic>{
        "transports": ["websocket"],
        "autoConnect": true,
      },
    );

    socket.onConnect((_) {
      isConnected = true;
      print("Connected to backend socket");
      requestStatus(); // Ask backend for initial status
    });

    // Use the passed callback instead of just printing
    socket.on("scan_status", (data) {
      print("Got scan_status: $data"); // Optional debug
      onScanStatus(data); // Call your UI update
    });

    socket.onDisconnect((_) {
      isConnected = false;
      print("Socket disconnected");
    });
  }

  void toggleScan() {
    if (isConnected) {
      socket.emit("toggle_scan", {"toggle": true});
    } else {
      print("Socket not connected!");
    }
  }

  void requestStatus() {
    if (isConnected) {
      socket.emit("get_status", {"toggle": true});
    }
  }

  void disconnect() {
    socket.disconnect();
  }
}
