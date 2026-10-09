export type InterviewerPersonaId = 'recruiter' | 'manager' | 'hr'

export const INTERVIEWER_PERSONAS = Object.freeze([
  Object.freeze({
    id: 'recruiter',
    name: 'University Recruiter',
    role: 'Early-career hiring',
    tone: 'Polite',
    description: 'Warm and encouraging. Eases you into the conversation and roots for you.',
  }),
  Object.freeze({
    id: 'manager',
    name: 'Senior Manager',
    role: 'Hiring manager',
    tone: 'Formal',
    description: 'Measured and professional. Expects structure and clear reasoning behind your answer.',
  }),
  Object.freeze({
    id: 'hr',
    name: 'HR Lead',
    role: 'Compensation & policy',
    tone: 'Firm',
    description: 'Direct and policy-led. Holds the line on constraints and asks for a defensible case.',
  }),
] as const)

export function isInterviewerPersonaId(value: unknown): value is InterviewerPersonaId {
  return typeof value === 'string' && INTERVIEWER_PERSONAS.some((persona) => persona.id === value)
}

export function interviewerPersonaName(value: InterviewerPersonaId | null | undefined): string | null {
  return value == null ? null : INTERVIEWER_PERSONAS.find((persona) => persona.id === value)?.name ?? null
}
