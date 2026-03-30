import 'package:flutter/material.dart';
import '../theme.dart';

/// Shows the last user query and the last agent response.
class TranscriptWidget extends StatelessWidget {
  final String userText;
  final String agentText;
  final String modelBadge;

  const TranscriptWidget({
    super.key,
    required this.userText,
    required this.agentText,
    this.modelBadge = '',
  });

  @override
  Widget build(BuildContext context) {
    if (userText.isEmpty && agentText.isEmpty) return const SizedBox.shrink();

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 24),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFF080816),
        border: Border.all(color: kCyanDim.withOpacity(0.2)),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (userText.isNotEmpty) ...[
            Text('YOU', style: Theme.of(context).textTheme.labelSmall),
            const SizedBox(height: 4),
            Text(userText, style: Theme.of(context).textTheme.bodyMedium),
            const SizedBox(height: 12),
          ],
          if (agentText.isNotEmpty) ...[
            Row(
              children: [
                Text('JARVIS', style: Theme.of(context).textTheme.labelSmall),
                if (modelBadge.isNotEmpty) ...[
                  const SizedBox(width: 8),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                    decoration: BoxDecoration(
                      border: Border.all(color: kCyanDim.withOpacity(0.4)),
                      borderRadius: BorderRadius.circular(2),
                    ),
                    child: Text(
                      modelBadge,
                      style: const TextStyle(
                        color: kCyanDim,
                        fontSize: 9,
                        letterSpacing: 1,
                      ),
                    ),
                  ),
                ],
              ],
            ),
            const SizedBox(height: 4),
            Text(agentText, style: Theme.of(context).textTheme.bodyMedium),
          ],
        ],
      ),
    );
  }
}
