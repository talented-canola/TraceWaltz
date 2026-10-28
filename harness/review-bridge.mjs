// Additional headless entry for the existing EvoTrace /review command (DSH rc.8).
import { randomUUID } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

export const name = 'evotrace-bridge-review'
export const inject = ['agents', 'sessions', 'agentDefaultModel', 'commands', 'agentPresets']

export async function runReview(ctx, config, installModelSelection, createUserMessage) {
  if (!/^[A-Za-z0-9_-]+$/.test(config.candidate)) throw new Error('invalid candidate')
  const selection = ctx.agentDefaultModel.currentSelection()
  const previous = config.resume ? JSON.parse(await readFile(config.receipt, 'utf8')) : undefined
  const options = {
    agentOptions: { provider: selection.provider, model: selection.model },
    setup: async agentCtx => {
      await ctx.agentPresets.mount(agentCtx, 'evotrace-orchestrator')
      installModelSelection(agentCtx, { current: selection, assembled: undefined })
    },
  }
  const { agent } = previous
    ? await ctx.agents.resume({ ...options, resumeSessionId: previous.session_id })
    : await ctx.agents.create({ ...options, sessionId: `session-${randomUUID()}`, meta: { cwd: process.cwd() } })
  await agent.whenIdle()
  let execution = previous?.command_execution
  if (previous) {
    agent.followup(createUserMessage({ content: [{ type: 'text', text:
      `Continue the existing EvoTrace review for ${config.candidate}, using the original review token ${previous.review_token}. `
      + (config.resumeNote || 'Continue from the latest persisted reports after the inspected execution problem was repaired. Preserve the recorded route and lineage; repeat affected downstream stages when their inputs changed.') }],
      source: { kind: 'user' } }))
  } else {
    execution = await ctx.commands.execute(agent, `/review ${config.candidate}`, [], new AbortController().signal)
  }
  if (!execution || execution.result?.kind === 'error') throw new Error('review command rejected: ' + JSON.stringify(execution))
  const token = execution.result?.text?.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/)?.[0]
  if (!token) throw new Error('review command returned no bound review token')
  await agent.whenIdle()
  await ctx.sessions.flush(agent.session)
  // This is command completion, not an assertion that all four roles or Docker validation passed.
  const receipt = { version: 1, candidate: config.candidate, session_id: agent.id, review_token: token,
    command_execution: execution, status: 'command_completed' }
  await mkdir(dirname(config.receipt), { recursive: true })
  await writeFile(config.receipt, JSON.stringify(receipt) + '\n', 'utf8')
  return receipt
}

export function apply(ctx, config) {
  const exit = ctx.get('appExit')
  if (!exit || !config.moduleRoot || !config.receipt) throw new Error('missing review bridge configuration')
  ;(async () => {
    await ctx.get('loader')?.await()
    const require = createRequire(config.moduleRoot + '/package.json')
    const { installModelSelection } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-agent')).href)
    const { createUserMessage } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href)
    await runReview(ctx, config, installModelSelection, createUserMessage)
    exit(0)
  })().catch(error => { process.stderr.write(`review bridge: ${error.stack || error.message}\n`); exit(1) })
}
