class AuthService {
  static final AuthService _instance = AuthService._internal();
  factory AuthService() => _instance;
  AuthService._internal();

  bool _isAdmin = false;

  bool get isAdmin => _isAdmin;

  bool login(String password) {
    if (password == "admin123") { // local password
      _isAdmin = true;
      return true;
    }
    return false;
  }

  void logout() {
    _isAdmin = false;
  }
}
