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
      requestStatus(); // initial fetch
    });

    socket.onDisconnect((_) {
      isConnected = false;
      print("Socket disconnected");
    });
  }

  void listenScanStatus(Function(dynamic) onScanStatus) {
    socket.on("scan_status", (data) {
      onScanStatus(data);

      // stop listening if running is false
      if (!(data["running"] ?? false)) {
        print("[+] Scan finished, removing listener");
        socket.off("scan_status");
      }
    });
  }

  void toggleScan() {
    if (isConnected) {
      socket.emit("toggle_scan", {"toggle": true});
      print("[>] toggle_scan emitted");
    } else {
      print("Socket not connected!");
    }
  }

  void requestStatus() {
    if (isConnected) {
      socket.emit("get_status", {"toggle": true});
      print("[>] get_status emitted");
    }
  }

  void disconnect() {
    socket.disconnect();
  }
}
