import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPollGuard } from '../src/pollingGuard.js'

test('begins normally when not busy', () => {
  const guard = createPollGuard()
  const handle = guard.begin({ skipBusy: true })
  assert.ok(handle)
  assert.equal(handle.isCurrent(), true)
})

test('skips when busy and skipBusy is set', () => {
  const guard = createPollGuard()
  assert.ok(guard.begin({ skipBusy: true }))
  assert.equal(guard.begin({ skipBusy: true }), null)
})

test('does not skip a non-skipBusy request while busy', () => {
  const guard = createPollGuard()
  guard.begin({ skipBusy: true })
  const manual = guard.begin()
  assert.ok(manual)
  assert.equal(manual.isCurrent(), true)
})

test('older request cannot write after a newer one started', () => {
  const guard = createPollGuard()
  const poll = guard.begin({ skipBusy: true })
  const manual = guard.begin()
  assert.equal(poll.isCurrent(), false)
  assert.equal(manual.isCurrent(), true)
  guard.end(poll.gen)
  assert.equal(manual.isCurrent(), true)
  guard.end(manual.gen)
  assert.ok(guard.begin({ skipBusy: true }))
})

test('end only clears busy flag for the matching generation', () => {
  const guard = createPollGuard()
  const poll = guard.begin({ skipBusy: true })
  guard.begin()
  guard.end(poll.gen)
  assert.equal(guard.begin({ skipBusy: true }), null)
  guard.end(poll.gen)
  assert.equal(guard.begin({ skipBusy: true }), null)
})