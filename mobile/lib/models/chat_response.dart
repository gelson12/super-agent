class ChatResponse {
  final String response;
  final String modelUsed;
  final String routedBy;
  final int confidenceScore;
  final bool wasReviewed;
  final bool isEnsemble;

  const ChatResponse({
    required this.response,
    required this.modelUsed,
    required this.routedBy,
    this.confidenceScore = 100,
    this.wasReviewed = false,
    this.isEnsemble = false,
  });

  factory ChatResponse.fromJson(Map<String, dynamic> json) => ChatResponse(
        response: json['response'] as String? ?? '',
        modelUsed: json['model_used'] as String? ?? 'UNKNOWN',
        routedBy: json['routed_by'] as String? ?? '',
        confidenceScore: json['confidence_score'] as int? ?? 100,
        wasReviewed: json['was_reviewed'] as bool? ?? false,
        isEnsemble: json['is_ensemble'] as bool? ?? false,
      );
}
