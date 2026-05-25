#!/usr/bin/env node
/**
 * Smoke test: findLargestEmptyRegion classifies known patterns correctly.
 * Run: node tests/test_local_emptiness.js
 *
 * Note: server.js boots an express listener on import so we inline a copy
 * of the function under test here. If the production implementation in
 * server.js changes, update this copy in lockstep.
 */
'use strict'

// --- BEGIN COPY (must match server.js exactly) ---
const GRID_COLS = 6
const GRID_ROWS = 4
const SLIDE_W_INCH = 13.333
const SLIDE_H_INCH = 7.5
const BODY_Y_START_INCH = 0.84

function findLargestEmptyRegion(shapes) {
  const bodyH = SLIDE_H_INCH - BODY_Y_START_INCH
  const cellW = SLIDE_W_INCH / GRID_COLS
  const cellH = bodyH / GRID_ROWS
  const grid = Array.from({ length: GRID_ROWS }, () => Array(GRID_COLS).fill(0))
  for (const s of shapes) {
    if (s.y + s.h <= BODY_Y_START_INCH) continue
    const yEff = Math.max(s.y, BODY_Y_START_INCH)
    const c1 = Math.max(0, Math.floor(s.x / cellW))
    const c2 = Math.min(GRID_COLS - 1, Math.floor((s.x + s.w - 0.01) / cellW))
    const r1 = Math.max(0, Math.floor((yEff - BODY_Y_START_INCH) / cellH))
    const r2 = Math.min(
      GRID_ROWS - 1,
      Math.floor((s.y + s.h - 0.01 - BODY_Y_START_INCH) / cellH),
    )
    for (let r = r1; r <= r2; r++) {
      for (let c = c1; c <= c2; c++) grid[r][c] = 1
    }
  }
  const visited = Array.from({ length: GRID_ROWS }, () =>
    Array(GRID_COLS).fill(false),
  )
  let maxRegion = 0
  for (let r = 0; r < GRID_ROWS; r++) {
    for (let c = 0; c < GRID_COLS; c++) {
      if (grid[r][c] === 0 && !visited[r][c]) {
        const stack = [[r, c]]
        let size = 0
        while (stack.length) {
          const [rr, cc] = stack.pop()
          if (rr < 0 || rr >= GRID_ROWS || cc < 0 || cc >= GRID_COLS) continue
          if (visited[rr][cc] || grid[rr][cc] === 1) continue
          visited[rr][cc] = true
          size++
          stack.push([rr + 1, cc], [rr - 1, cc], [rr, cc + 1], [rr, cc - 1])
        }
        if (size > maxRegion) maxRegion = size
      }
    }
  }
  return maxRegion
}
// --- END COPY ---

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg)
    process.exit(1)
  }
  console.log('ok:', msg)
}

// case 1: empty slide -> all 24 cells empty
{
  const shapes = []
  const got = findLargestEmptyRegion(shapes)
  assert(got === 24, `empty slide: got ${got}, expected 24`)
}

// case 2: full-coverage slide (one big shape covering body)
{
  const shapes = [{ x: 0, y: 0.84, w: 13.333, h: 6.66 }]
  const got = findLargestEmptyRegion(shapes)
  assert(got === 0, `full coverage: got ${got}, expected 0`)
}

// case 3: top-half covered, bottom-half empty (classic v2 problem)
{
  const shapes = [{ x: 0, y: 0.84, w: 13.333, h: 3.33 }] // upper half only
  const got = findLargestEmptyRegion(shapes)
  // bottom 2 rows × 6 cols = 12 cells empty
  assert(got >= 11 && got <= 13, `bottom-half empty: got ${got}, expected ~12`)
}

// case 4: scattered small shapes, large central void
{
  const shapes = [
    { x: 0, y: 0.84, w: 2.0, h: 1.0 }, // top-left
    { x: 11, y: 0.84, w: 2.0, h: 1.0 }, // top-right
    { x: 0, y: 6.0, w: 2.0, h: 1.0 }, // bottom-left
    { x: 11, y: 6.0, w: 2.0, h: 1.0 }, // bottom-right
  ]
  const got = findLargestEmptyRegion(shapes)
  // huge central void
  assert(got >= 8, `corners-only: got ${got}, expected >= 8`)
}

// case 5: header-only shape (above body) doesn't pollute body grid
{
  const shapes = [{ x: 0, y: 0, w: 13.333, h: 0.8 }] // entirely above 0.84
  const got = findLargestEmptyRegion(shapes)
  assert(got === 24, `header-only shape: got ${got}, expected 24 (body untouched)`)
}

console.log('all local-emptiness tests passed')
