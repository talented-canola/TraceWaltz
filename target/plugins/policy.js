import { readFileSync } from 'node:fs'

export const name = 'coding-policy'
export const inject = ['systemPrompt']
export function apply(ctx) {
  ctx.systemPrompt.section({
    name: 'harness:coding-policy', order: 50,
    text: readFileSync(new URL('../prompts/system.md', import.meta.url), 'utf8'),
  })
}
