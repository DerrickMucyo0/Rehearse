import asyncio
from app.diagnosis import DiagnosisContext
from app.judge import score as judge_score
from app.semantic_diagnosis import SemanticDiagnosis

class SpeakUpDiagnosisAdapter:
    async def diagnose(self, context: DiagnosisContext) -> SemanticDiagnosis:
        rubric_scores = await asyncio.to_thread(judge_score, context.answer)
        
        addressed = "yes" if rubric_scores.get("clarity", 0.0) >= 0.7 else ("partially" if rubric_scores.get("clarity", 0.0) >= 0.4 else "no")
        structure_val = "clear" if rubric_scores.get("organization", 0.0) >= 0.7 else ("mixed" if rubric_scores.get("organization", 0.0) >= 0.4 else "unclear")
        
        return SemanticDiagnosis(
            addressed_question=addressed,
            addressed_question_reason=f"Clarity score: {rubric_scores.get('clarity', 0.0):.2f}",
            strengths=tuple(["Showed composure under pressure."] if rubric_scores.get("composure", 0.0) > 0.6 else []),
            missing_information=tuple(["Lacked detailed justification."] if rubric_scores.get("argument_strength", 0.0) < 0.6 else []),
            structure=structure_val,
            structure_feedback=f"Organization score: {rubric_scores.get('organization', 0.0):.2f}",
            next_focus="structure" if structure_val != "clear" else "specificity",
            next_focus_reason="Focus on clear, undeniable justification.",
            retry_instruction="Try answering again, focusing strictly on your weakest scoring area.",
        )
