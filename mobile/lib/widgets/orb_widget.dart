import 'package:flutter/material.dart';
import '../theme.dart';

enum JarvisState { idle, wakeDetected, listening, processing, speaking }

/// The central glowing orb — the visual heartbeat of the JARVIS interface.
/// Its size, brightness, and animation change with [state].
class OrbWidget extends StatefulWidget {
  final JarvisState state;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  const OrbWidget({
    super.key,
    required this.state,
    this.onTap,
    this.onLongPress,
  });

  @override
  State<OrbWidget> createState() => _OrbWidgetState();
}

class _OrbWidgetState extends State<OrbWidget>
    with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;
  late Animation<double> _pulse;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat(reverse: true);

    _pulse = Tween<double>(begin: 0.9, end: 1.1).animate(
      CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: widget.onTap,
      onLongPress: widget.onLongPress,
      child: AnimatedBuilder(
        animation: _ctrl,
        builder: (_, __) {
          final props = _propsForState(widget.state);
          final scale = _shouldPulse(widget.state) ? _pulse.value : 1.0;
          final spin = widget.state == JarvisState.processing;

          return Transform.scale(
            scale: scale,
            child: _buildOrb(props, spin),
          );
        },
      ),
    );
  }

  Widget _buildOrb(_OrbProps p, bool spin) {
    final orb = Container(
      width: p.size,
      height: p.size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: kDarkBg,
        boxShadow: [
          BoxShadow(
            color: kCyan.withOpacity(p.glowOpacity),
            spreadRadius: p.spread,
            blurRadius: p.blur,
          ),
          BoxShadow(
            color: kBlue.withOpacity(p.glowOpacity * 0.4),
            spreadRadius: p.spread * 0.5,
            blurRadius: p.blur * 1.5,
          ),
        ],
        border: Border.all(
          color: kCyan.withOpacity(p.borderOpacity),
          width: 1.5,
        ),
      ),
      child: Center(
        child: Container(
          width: p.size * 0.55,
          height: p.size * 0.55,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: kCyan.withOpacity(p.innerOpacity),
          ),
        ),
      ),
    );

    if (!spin) return orb;

    return RotationTransition(
      turns: _ctrl,
      child: CustomPaint(
        painter: _RingPainter(),
        child: orb,
      ),
    );
  }

  bool _shouldPulse(JarvisState s) =>
      s == JarvisState.listening ||
      s == JarvisState.speaking ||
      s == JarvisState.wakeDetected;

  _OrbProps _propsForState(JarvisState s) {
    switch (s) {
      case JarvisState.idle:
        return _OrbProps(size: 160, glowOpacity: 0.25, spread: 4, blur: 20, innerOpacity: 0.06, borderOpacity: 0.3);
      case JarvisState.wakeDetected:
        return _OrbProps(size: 190, glowOpacity: 0.9, spread: 16, blur: 48, innerOpacity: 0.35, borderOpacity: 0.9);
      case JarvisState.listening:
        return _OrbProps(size: 175, glowOpacity: 0.65, spread: 10, blur: 36, innerOpacity: 0.2, borderOpacity: 0.7);
      case JarvisState.processing:
        return _OrbProps(size: 165, glowOpacity: 0.4, spread: 6, blur: 24, innerOpacity: 0.1, borderOpacity: 0.5);
      case JarvisState.speaking:
        return _OrbProps(size: 180, glowOpacity: 0.55, spread: 8, blur: 32, innerOpacity: 0.18, borderOpacity: 0.6);
    }
  }
}

class _OrbProps {
  final double size, glowOpacity, spread, blur, innerOpacity, borderOpacity;
  const _OrbProps({
    required this.size,
    required this.glowOpacity,
    required this.spread,
    required this.blur,
    required this.innerOpacity,
    required this.borderOpacity,
  });
}

// Spinning arc ring shown during PROCESSING state
class _RingPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = kCyan.withOpacity(0.4)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5;
    canvas.drawArc(
      Rect.fromLTWH(4, 4, size.width - 8, size.height - 8),
      0, 4.2, false, paint,
    );
  }

  @override
  bool shouldRepaint(_) => false;
}
