import ts from 'typescript'
import { afterEach, expect, test, vi } from 'vitest'
import { personalizedDrillForFocus } from './personalizedDrills'
import type { DrillFocus, PersonalizedDrill } from './personalizedDrills'
import type { SemanticDiagnosis } from './interviewApi'
import semanticDiagnosisContract from './fixtures/semanticDiagnosis.v1.json?raw'
import drillModuleSource from './personalizedDrills.ts?raw'

const approvedCopy = {
  answer_the_question: {
    title: 'Answer the question first',
    goal: 'Make your direct answer clear before adding background.',
    steps: [
      'Read the question and identify what it asks you to explain.',
      'Prepare one opening sentence that directly answers it.',
      'Add one relevant example that supports that answer.',
    ],
  },
  specificity: {
    title: 'Make one example concrete',
    goal: 'Replace a broad statement with specific facts about your contribution.',
    steps: [
      'Choose one general statement in your answer.',
      'Identify the situation and the action you personally took.',
      'Rehearse that part using concrete facts you can accurately describe.',
    ],
  },
  supporting_detail: {
    title: 'Support one claim',
    goal: 'Connect an assertion to evidence or a concrete example.',
    steps: [
      'Choose one important claim in your answer.',
      'Identify an observation, example, or result that supports it.',
      'Explain the connection and acknowledge any limits in the evidence.',
    ],
  },
  structure: {
    title: 'Put the story in order',
    goal: 'Make the account easier to follow.',
    steps: [
      'Outline the situation, your actions, and the result.',
      'Arrange the facts in that order.',
      'Rehearse the answer using that outline and clear transitions.',
    ],
  },
  completeness: {
    title: 'Fill the missing part',
    goal: 'Cover the important components requested by the question.',
    steps: [
      'Identify the components the question asks for.',
      'Check which components your answer already covers.',
      'Add the missing facts you know, or clearly acknowledge what you cannot establish.',
    ],
  },
  conciseness: {
    title: 'Keep the core, cut repetition',
    goal: 'Preserve the useful facts while removing repetition and unrelated detail.',
    steps: [
      'Identify your direct answer and the essential supporting facts.',
      'Remove repeated points and background that does not help answer the question.',
      'Rehearse the shorter version without losing those essential facts.',
    ],
  },
  maintain_strengths: {
    title: 'Repeat what worked',
    goal: 'Preserve the effective parts of your answer in another attempt.',
    steps: [
      'Identify one strength described in your feedback.',
      'Outline the facts and structure that demonstrate that strength.',
      'Rehearse again while preserving that strength and factual accuracy.',
    ],
  },
} satisfies Record<DrillFocus, Pick<PersonalizedDrill, 'title' | 'goal' | 'steps'>>

const focuses = Object.keys(approvedCopy) as DrillFocus[]
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('covers all seven validated focus values with one distinct drill per focus', () => {
  expect(focuses).toHaveLength(7)
  expect(new Set(focuses.map((focus) => personalizedDrillForFocus(focus).focus))).toEqual(new Set(focuses))
})

test.each(focuses)('%s has exactly the approved version, focus, copy and three nonblank steps', (focus) => {
  const drill = personalizedDrillForFocus(focus)
  expect(drill).toEqual({ drill_version: 'personalized-drill-v1', focus, ...approvedCopy[focus] })
  expect(Object.keys(drill).sort()).toEqual(['drill_version', 'focus', 'goal', 'steps', 'title'])
  expect(drill.steps).toHaveLength(3)
  for (const value of [drill.drill_version, drill.focus, drill.title, drill.goal, ...drill.steps]) {
    expect(typeof value).toBe('string')
    expect(value.trim().length).toBeGreaterThan(0)
  }
})

test.each(focuses)('%s is deterministic across repeated and interleaved selections', (focus) => {
  const before = JSON.stringify(personalizedDrillForFocus(focus))
  for (let repetition = 0; repetition < 3; repetition += 1) {
    for (const otherFocus of focuses) personalizedDrillForFocus(otherFocus)
    expect(JSON.stringify(personalizedDrillForFocus(focus))).toBe(before)
  }
})

test.each(focuses)('%s cannot be mutated to corrupt subsequent selections', (focus) => {
  const drill = personalizedDrillForFocus(focus)
  expect(Object.isFrozen(drill)).toBe(true)
  expect(Object.isFrozen(drill.steps)).toBe(true)
  expect(Reflect.set(drill, 'title', 'CORRUPTED TITLE')).toBe(false)
  expect(Reflect.set(drill, 'steps', ['CORRUPTED STEP'])).toBe(false)
  expect(Reflect.set(drill.steps, '0', 'CORRUPTED STEP')).toBe(false)
  expect(Reflect.set(drill.steps, 'length', 0)).toBe(false)
  expect(personalizedDrillForFocus(focus)).toEqual({ drill_version: 'personalized-drill-v1', focus, ...approvedCopy[focus] })
})

test.each([
  { label: 'null', value: null },
  { label: 'undefined', value: undefined },
  { label: 'empty string', value: '' },
  { label: 'unknown focus', value: 'confidence' },
  { label: 'uppercase focus', value: 'SPECIFICITY' },
  { label: 'padded focus', value: ' specificity ' },
  { label: '__proto__', value: '__proto__' },
  { label: 'constructor', value: 'constructor' },
  { label: 'prototype', value: 'prototype' },
  { label: 'toString', value: 'toString' },
  { label: 'toLocaleString', value: 'toLocaleString' },
  { label: 'valueOf', value: 'valueOf' },
  { label: 'hasOwnProperty', value: 'hasOwnProperty' },
  { label: 'isPrototypeOf', value: 'isPrototypeOf' },
  { label: 'number', value: 0 },
  { label: 'boolean', value: false },
  { label: 'object', value: {} },
  { label: 'empty array', value: [] },
  { label: 'array containing a valid focus', value: ['specificity'] },
  { label: 'boxed valid focus', value: Object('specificity') },
  { label: 'object coercible to a valid focus', value: { toString: () => 'specificity' } },
  { label: 'object with primitive coercion', value: { [Symbol.toPrimitive]: () => 'specificity' } },
  { label: 'symbol', value: Symbol('specificity') },
  { label: 'function', value: () => 'specificity' },
])('rejects unsupported runtime focus $label without a fallback or coercion', ({ value }) => {
  expect(() => personalizedDrillForFocus(value as DrillFocus)).toThrowError(/^Unsupported drill focus\.$/)
})

test('selection makes no network, storage, clock or random calls', () => {
  const networkCall = vi.fn(() => { throw new Error('Network access is forbidden.') })
  const storageAccess = vi.fn(() => { throw new Error('Storage access is forbidden.') })
  const storage = new Proxy({}, { get: storageAccess, set: storageAccess })
  const clockCall = vi.fn(() => { throw new Error('Clock access is forbidden.') })
  const clockNow = vi.fn(() => { throw new Error('Clock access is forbidden.') })
  const randomCall = vi.fn(() => { throw new Error('Random selection is forbidden.') })
  const randomSpy = vi.spyOn(Math, 'random').mockImplementation(randomCall)
  for (const name of ['fetch', 'XMLHttpRequest', 'WebSocket']) vi.stubGlobal(name, networkCall)
  for (const name of ['localStorage', 'sessionStorage', 'indexedDB']) vi.stubGlobal(name, storage)
  vi.stubGlobal('Date', Object.assign(clockCall, { now: clockNow }))
  const selected: PersonalizedDrill[] = []
  try {
    for (const focus of focuses) selected.push(personalizedDrillForFocus(focus))
  } finally {
    vi.unstubAllGlobals()
    randomSpy.mockRestore()
  }
  expect(selected.map((drill) => drill.focus)).toEqual(focuses)
  for (const forbiddenCall of [networkCall, storageAccess, clockCall, clockNow, randomCall]) {
    expect(forbiddenCall).not.toHaveBeenCalled()
  }
})

test('selecting from validated next_focus leaves the semantic diagnosis unchanged', () => {
  const diagnosis = JSON.parse(semanticDiagnosisContract) as SemanticDiagnosis
  const before = structuredClone(diagnosis)
  expect(personalizedDrillForFocus(diagnosis.next_focus).focus).toBe(diagnosis.next_focus)
  expect(diagnosis).toEqual(before)
})

test('the module has only a type dependency and no React, backend or side-effect APIs', () => {
  const source = ts.createSourceFile('personalizedDrills.ts',
    drillModuleSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const imports = source.statements.filter(ts.isImportDeclaration)
  expect(imports).toHaveLength(1)
  expect(imports[0].importClause?.isTypeOnly).toBe(true)
  expect(ts.isStringLiteral(imports[0].moduleSpecifier) && imports[0].moduleSpecifier.text).toBe('./interviewApi')
  const forbiddenIdentifiers = new Set([
    'React', 'useState', 'useReducer', 'useEffect', 'useLayoutEffect', 'fetch', 'XMLHttpRequest', 'WebSocket',
    'localStorage', 'sessionStorage', 'indexedDB', 'navigator', 'Date', 'performance', 'setTimeout', 'setInterval', 'require',
  ])
  const forbiddenReferences: string[] = []
  function inspect(node: ts.Node) {
    if (ts.isIdentifier(node) && forbiddenIdentifiers.has(node.text)) forbiddenReferences.push(node.text)
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) forbiddenReferences.push('dynamic import')
    if (ts.isExportDeclaration(node) && node.moduleSpecifier) forbiddenReferences.push('module re-export')
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) &&
      node.expression.text === 'Math' && node.name.text === 'random') forbiddenReferences.push('Math.random')
    ts.forEachChild(node, inspect)
  }
  inspect(source)
  expect(forbiddenReferences).toEqual([])
})
