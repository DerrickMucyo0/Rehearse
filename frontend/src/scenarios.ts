export type ScenarioType = 'job_interview' | 'public_speaking' | 'thesis_defense' | 'salary_negotiation'

export const SCENARIOS = Object.freeze([
  Object.freeze({ type: 'job_interview', label: 'Job Interview', description: 'Practice explaining your experience, strengths, and fit for a role.' }),
  Object.freeze({ type: 'public_speaking', label: 'Public Speaking', description: 'Practice introducing ideas and speaking clearly to an audience.' }),
  Object.freeze({ type: 'thesis_defense', label: 'Thesis Defense', description: 'Practice explaining your research and responding to questions.' }),
  Object.freeze({ type: 'salary_negotiation', label: 'Salary Negotiation', description: 'Practice discussing your value and compensation expectations.' }),
] as const)

export function isScenarioType(value: unknown): value is ScenarioType {
  return typeof value === 'string' && SCENARIOS.some((scenario) => scenario.type === value)
}

export function scenarioLabel(type: ScenarioType): string {
  return SCENARIOS.find((scenario) => scenario.type === type)!.label
}
