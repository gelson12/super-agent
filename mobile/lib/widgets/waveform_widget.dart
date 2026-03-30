import 'dart:math';
import 'package:flutter/material.dart';
import '../theme.dart';
import 'orb_widget.dart';

/// Animated waveform bars — active when LISTENING or SPEAKING.
class WaveformWidget extends StatefulWidget {
  final JarvisState state;
  const WaveformWidget({super.key, required this.state});

  @override
  State<WaveformWidget> createState() => _WaveformWidgetState();
}

class _WaveformWidgetState extends State<WaveformWidget>
    with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;
  final _rng = Random();
  static const _bars = 24;
  final _heights = List<double>.filled(_bars, 4);

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 120),
    )..addListener(_randomise)..repeat();
  }

  void _randomise() {
    if (!_isActive) return;
    setState(() {
      for (var i = 0; i < _bars; i++) {
        _heights[i] = 4 + _rng.nextDouble() * 36;
      }
    });
  }

  bool get _isActive =>
      widget.state == JarvisState.listening ||
      widget.state == JarvisState.speaking ||
      widget.state == JarvisState.wakeDetected;

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 48,
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        crossAxisAlignment: CrossAxisAlignment.center,
        children: List.generate(_bars, (i) {
          final h = _isActive ? _heights[i] : 4.0;
          return AnimatedContainer(
            duration: const Duration(milliseconds: 100),
            width: 3,
            height: h,
            margin: const EdgeInsets.symmetric(horizontal: 1.5),
            decoration: BoxDecoration(
              color: kCyan.withOpacity(_isActive ? 0.75 : 0.2),
              borderRadius: BorderRadius.circular(2),
            ),
          );
        }),
      ),
    );
  }
}
