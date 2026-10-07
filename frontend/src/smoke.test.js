import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'

test('project-local React dependency loads', () => {
  assert.equal(createElement('span').type, 'span')
})
