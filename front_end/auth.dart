class AuthService {
  static final AuthService _instance = AuthService._internal();
  factory AuthService() => _instance;
  AuthService._internal();

  bool _isAdmin = false;

  bool get isAdmin => _isAdmin;

  // A2: password driven by --dart-define=FRONT_ADMIN_PASSWORD=<value> at build
  // time, matching back_end/secrets.py PHONEBOX_FRONT_ADMIN_PASSWORD env var.
  //
  // Dev / emulator:
  //   flutter run                         → uses 'admin123' default
  //
  // Production tablet build:
  //   flutter build apk \
  //     --dart-define=FRONT_ADMIN_PASSWORD=<real_password>
  //
  // The literal password is baked into the compiled APK (not plain-text in
  // source).  For a school intranet this is an acceptable risk; the same
  // string is also in the server env var.  If you need stronger separation,
  // drive the password from a /api/config endpoint hit at app startup instead.
  static const String _kPassword = String.fromEnvironment(
    'FRONT_ADMIN_PASSWORD',
    defaultValue: 'admin123',
  );

  bool login(String password) {
    if (password == _kPassword) {
      _isAdmin = true;
      return true;
    }
    return false;
  }

  void logout() {
    _isAdmin = false;
  }
}
