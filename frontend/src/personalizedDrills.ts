import type { SemanticDiagnosis } from './interviewApi'

export type DrillFocus = SemanticDiagnosis['next_focus']

export type PersonalizedDrill = Readonly<{
  drill_version: 'personalized-drill-v1'
  focus: DrillFocus
  title: string
  goal: string
  steps: readonly [string, string, string]
}>

const PERSONALIZED_DRILLS = {
  answer_the_question: {
    drill_version: 'personalized-drill-v1',
    focus: 'answer_the_question',
    title: 'Answer the question first',
    goal: 'Make your direct answer clear before adding background.',
    steps: [
      'Read the question and identify what it asks you to explain.',
      'Prepare one opening sentence that directly answers it.',
      'Add one relevant example that supports that answer.',
    ],
  },
  specificity: {
    drill_version: 'personalized-drill-v1',
    focus: 'specificity',
    title: 'Make one example concrete',
    goal: 'Replace a broad statement with specific facts about your contribution.',
    steps: [
      'Choose one general statement in your answer.',
      'Identify the situation and the action you personally took.',
      'Rehearse that part using concrete facts you can accurately describe.',
    ],
  },
  supporting_detail: {
    drill_version: 'personalized-drill-v1',
    focus: 'supporting_detail',
    title: 'Support one claim',
    goal: 'Connect an assertion to evidence or a concrete example.',
    steps: [
      'Choose one important claim in your answer.',
      'Identify an observation, example, or result that supports it.',
      'Explain the connection and acknowledge any limits in the evidence.',
    ],
  },
  structure: {
    drill_version: 'personalized-drill-v1',
    focus: 'structure',
    title: 'Put the story in order',
    goal: 'Make the account easier to follow.',
    steps: [
      'Outline the situation, your actions, and the result.',
      'Arrange the facts in that order.',
      'Rehearse the answer using that outline and clear transitions.',
    ],
  },
  completeness: {
    drill_version: 'personalized-drill-v1',
    focus: 'completeness',
    title: 'Fill the missing part',
    goal: 'Cover the important components requested by the question.',
    steps: [
      'Identify the components the question asks for.',
      'Check which components your answer already covers.',
      'Add the missing facts you know, or clearly acknowledge what you cannot establish.',
    ],
  },
  conciseness: {
    drill_version: 'personalized-drill-v1',
    focus: 'conciseness',
    title: 'Keep the core, cut repetition',
    goal: 'Preserve the useful facts while removing repetition and unrelated detail.',
    steps: [
      'Identify your direct answer and the essential supporting facts.',
      'Remove repeated points and background that does not help answer the question.',
      'Rehearse the shorter version without losing those essential facts.',
    ],
  },
  maintain_strengths: {
    drill_version: 'personalized-drill-v1',
    focus: 'maintain_strengths',
    title: 'Repeat what worked',
    goal: 'Preserve the effective parts of your answer in another attempt.',
    steps: [
      'Identify one strength described in your feedback.',
      'Outline the facts and structure that demonstrate that strength.',
      'Rehearse again while preserving that strength and factual accuracy.',
    ],
  },
} satisfies { readonly [Focus in DrillFocus]: PersonalizedDrill & { readonly focus: Focus } }

// Freeze both levels so callers cannot change any future selection.
for (const drill of Object.values(PERSONALIZED_DRILLS)) {
  Object.freeze(drill.steps)
  Object.freeze(drill)
}
Object.freeze(PERSONALIZED_DRILLS)

export function personalizedDrillForFocus(focus: DrillFocus): PersonalizedDrill {
  if (typeof focus !== 'string' || !Object.hasOwn(PERSONALIZED_DRILLS, focus)) {
    throw new Error('Unsupported drill focus.')
  }
  return PERSONALIZED_DRILLS[focus]
}
