import test from 'node:test'
import assert from 'node:assert/strict'
import { GRAPH_GEOMETRY, layoutGraph, graphBounds, edgeGeometry, canvasPoint, canvasDelta, initialGraph } from '../src/quickBuilder/graph.js'

const node = (id, kind = 'format') => ({
  id, kind, label: `步驟 ${id}`, position: { x: 0, y: 100 },
  config: { text: '{{text}}', enabled: false, count: 3, nested: { values: ['甲'] } },
})
const edge = (source, target, branch) => ({ source, target, ...(branch ? { branch } : {}) })
const positions = graph => Object.fromEntries(graph.nodes.map(item => [item.id, item.position]))
function assertNoOverlap(graph, sizes = {}) {
  for (const a of graph.nodes) for (const b of graph.nodes) {
    if (a.id >= b.id) continue
    const aw = sizes[a.id]?.width || GRAPH_GEOMETRY.nodeWidth
    const ah = sizes[a.id]?.height || GRAPH_GEOMETRY.nodeHeight
    const bw = sizes[b.id]?.width || GRAPH_GEOMETRY.nodeWidth
    const bh = sizes[b.id]?.height || GRAPH_GEOMETRY.nodeHeight
    assert.ok(a.position.x + aw <= b.position.x || b.position.x + bw <= a.position.x || a.position.y + ah <= b.position.y || b.position.y + bh <= a.position.y, `${a.id}/${b.id} 不重疊`)
  }
}
function assertRouteClear(graph, route) {
  assert.ok(route, '存在避開節點的路徑')
  for (let index = 1; index < route.points.length; index++) {
    const a = route.points[index - 1], b = route.points[index]
    assert.ok(a.x === b.x || a.y === b.y, '每段皆為水平或垂直')
    for (const item of graph.nodes) {
      const { x, y } = item.position
      const width = GRAPH_GEOMETRY.nodeWidth, height = GRAPH_GEOMETRY.nodeHeight
      const crossing = a.x === b.x
        ? a.x > x && a.x < x + width && Math.max(a.y, b.y) > y && Math.min(a.y, b.y) < y + height
        : a.y > y && a.y < y + height && Math.max(a.x, b.x) > x && Math.min(a.x, b.x) < x + width
      assert.equal(crossing, false, `連線不穿過 ${item.id}`)
    }
  }
}
const diamond = () => ({
  nodes: [node('input', 'input'), node('condition', 'condition'), node('yes'), node('no'), node('join', 'model'), node('output', 'output')],
  edges: [edge('input', 'condition'), edge('condition', 'no', 'false'), edge('condition', 'yes', 'true'), edge('yes', 'join'), edge('no', 'join'), edge('join', 'output')],
})

test('六種內建範本的直鏈結構向下排列，保留節點設定且不改來源', () => {
  const kinds = [['prompt', 'mcp', 'model'], ['prompt', 'rag', 'model'], ['prompt', 'rag', 'model'], ['prompt', 'model', 'document'], ['prompt', 'mcp', 'model'], ['prompt', 'mcp', 'format']]
  for (const steps of kinds) {
    // Matches quick_templates._graph: input + three steps + output, x=index*260.
    const nodes = ['input', ...steps, 'output'].map((kind, index) => ({ ...node(`n${index + 1}`, kind), position: { x: index * 260, y: 100 } }))
    const graph = { nodes, edges: nodes.slice(1).map((item, index) => edge(nodes[index].id, item.id)) }
    const before = structuredClone(graph), arranged = layoutGraph(graph)
    assert.deepEqual(graph, before)
    assert.deepEqual(arranged.edges, before.edges)
    arranged.nodes.forEach((item, index) => {
      const { position, ...data } = item
      const { position: oldPosition, ...oldData } = before.nodes[index]
      assert.deepEqual(data, oldData)
      assert.deepEqual(position, { x: 24, y: 24 + index * 208 })
    })
    assertNoOverlap(arranged)
    arranged.nodes[0].config.nested.values.push('乙')
    assert.deepEqual(graph.nodes[0].config.nested.values, ['甲'])
  }
})

test('initialGraph不再三欄折返，成立左／不成立右且匯合置中，雙支總寬520', () => {
  const initial = initialGraph()
  assert.deepEqual(initial.nodes.map(item => item.kind), ['input', 'prompt', 'model', 'output'])
  assert.equal(new Set(initial.nodes.map(item => item.position.x)).size, 1)
  assert.ok(initial.nodes.every((item, index) => item.position.y === 24 + index * 208))
  const graph = layoutGraph(diamond()), p = positions(graph)
  assert.equal(p.yes.y, p.no.y)
  assert.ok(p.yes.x < p.no.x)
  assert.equal(p.join.x, (p.yes.x + p.no.x) / 2)
  assert.equal(p.condition.x, p.join.x)
  assert.equal(graphBounds(graph).width, 520)
  assertNoOverlap(graph)
})

test('節點連線亂序仍deterministic，重排相同且不增加schema欄位', () => {
  const original = diamond(), expected = layoutGraph(original)
  assert.deepEqual(positions(layoutGraph({ nodes: [...original.nodes].reverse(), edges: [...original.edges].reverse() })), positions(expected))
  assert.deepEqual(layoutGraph(expected), expected)
  assert.deepEqual(Object.keys(expected).sort(), ['edges', 'nodes'])
  expected.nodes.forEach(item => assert.deepEqual(Object.keys(item).sort(), ['config', 'id', 'kind', 'label', 'position']))
})

test('不等長分支的跳層線不穿過中間節點', () => {
  const graph = layoutGraph({ nodes: [node('in', 'input'), node('check', 'condition'), node('yes1'), node('yes2'), node('join', 'output')], edges: [edge('in', 'check'), edge('check', 'yes1', 'true'), edge('yes1', 'yes2'), edge('yes2', 'join'), edge('check', 'join', 'false')] })
  const p = positions(graph)
  assert.ok(p.join.y > p.yes2.y)
  assert.ok(p.yes1.x < p.check.x)
  assertNoOverlap(graph)
  graph.edges.forEach(item => assertRouteClear(graph, edgeGeometry(graph, item)))
})

test('兩條無關支線不因ID排序交叉；巢狀條件各自保留左右支線', () => {
  const simple = { nodes: ['a', 'b', 'x', 'y', 'out'].map(id => node(id)), edges: [edge('a', 'y'), edge('b', 'x'), edge('x', 'out'), edge('y', 'out')] }
  const p = positions(layoutGraph(simple))
  assert.equal(p.a.x, p.y.x)
  assert.equal(p.b.x, p.x.x)
  const nested = layoutGraph({
    nodes: ['in', 'outer', 'inner', 'right', 'yes', 'no', 'join'].map(id => node(id, ['outer', 'inner'].includes(id) ? 'condition' : 'format')),
    edges: [edge('in', 'outer'), edge('outer', 'inner', 'true'), edge('outer', 'right', 'false'), edge('inner', 'yes', 'true'), edge('inner', 'no', 'false'), edge('yes', 'join'), edge('no', 'join'), edge('right', 'join')],
  })
  const n = positions(nested)
  assert.ok(n.inner.x < n.right.x)
  assert.ok(n.yes.x < n.no.x)
  assertNoOverlap(nested)
  nested.edges.forEach(item => assertRouteClear(nested, edgeGeometry(nested, item)))
})

test('實測外框含padding高寬，不同節點尺寸仍不重疊', () => {
  const sizes = { yes: { width: 270, height: 240 }, condition: { width: 220, height: 180 } }
  const graph = layoutGraph(diamond(), { nodeSizes: sizes }), p = positions(graph)
  assertNoOverlap(graph, sizes)
  assert.equal(p.join.y, p.yes.y + 240 + GRAPH_GEOMETRY.rankGap)
  assert.equal(graphBounds(graph, { nodeSizes: sizes }).width, 590)
})

test('分支出口、標籤、匯合入口各自分開，所有路徑避開節點', () => {
  const graph = layoutGraph(diamond())
  const yes = edgeGeometry(graph, graph.edges.find(item => item.branch === 'true'))
  const no = edgeGeometry(graph, graph.edges.find(item => item.branch === 'false'))
  assert.ok(yes.start.x < no.start.x)
  assert.ok(yes.label.x < no.label.x)
  assert.equal(yes.label.y, yes.start.y + 24)
  const intoJoin = graph.edges.filter(item => item.target === 'join').map(item => edgeGeometry(graph, item))
  assert.notEqual(intoJoin[0].end.x, intoJoin[1].end.x)
  for (const item of graph.edges) {
    const route = edgeGeometry(graph, item)
    assert.match(route.path, /^M .* L /)
    assertRouteClear(graph, route)
  }
})

test('手動移動後長邊與反向邊繞過中間障礙物', () => {
  const graph = { nodes: [{ ...node('a'), position: { x: 24, y: 24 } }, { ...node('obstacle'), position: { x: 24, y: 232 } }, { ...node('b'), position: { x: 24, y: 440 } }], edges: [edge('a', 'b'), edge('b', 'a')] }
  graph.edges.forEach(item => assertRouteClear(graph, edgeGeometry(graph, item)))
  assert.ok(edgeGeometry(graph, graph.edges[0]).points.some(point => point.x <= 12 || point.x >= 236))
})

test('不存在端點或完全重疊無安全路徑時不畫假連線', () => {
  assert.equal(edgeGeometry({ nodes: [], edges: [] }, edge('a', 'b')), null)
  const graph = { nodes: [node('a'), node('b')], edges: [edge('a', 'b')] }
  assert.equal(edgeGeometry(graph, graph.edges[0]), null)
})

test('錯誤節點／循環拒絕整理，空圖與單節點仍有安全邊界', () => {
  assert.throws(() => layoutGraph({ nodes: [node('a'), node('a')], edges: [] }), /GRAPH_LAYOUT/)
  assert.throws(() => layoutGraph({ nodes: [node('a')], edges: [edge('a', 'missing')] }), /GRAPH_LAYOUT/)
  assert.throws(() => layoutGraph({ nodes: [node('a'), node('b')], edges: [edge('a', 'b'), edge('b', 'a')] }), /GRAPH_CYCLE/)
  assert.deepEqual(layoutGraph({ nodes: [], edges: [] }), { nodes: [], edges: [] })
  assert.deepEqual(graphBounds({ nodes: [], edges: [] }), { x: 0, y: 0, width: 48, height: 48 })
  assert.deepEqual(graphBounds(layoutGraph({ nodes: [node('a')], edges: [] })), { x: 0, y: 0, width: 248, height: 200 })
  assert.throws(() => layoutGraph(diamond(), { nodeWidth: 0 }), /GRAPH_GEOMETRY/)
})

test('縮放後拖入／拖動換算畫布座標，含viewport捲動且拒絕無效縮放', () => {
  assert.deepEqual(canvasPoint({ clientX: 260, clientY: 300 }, { left: 100, top: 80, scrollLeft: 40, scrollTop: 100, zoom: 0.5 }), { x: 400, y: 640 })
  assert.deepEqual(canvasDelta({ x: 32, y: -18 }, 0.5), { x: 64, y: -36 })
  assert.deepEqual(canvasDelta({ x: 32, y: -18 }, 2), { x: 16, y: -9 })
  assert.deepEqual(canvasPoint({ clientX: 260, clientY: 300 }, { left: 100, top: 80 }), { x: 160, y: 220 })
  assert.throws(() => canvasDelta({ x: 1, y: 1 }, 0), /CANVAS_ZOOM/)
  assert.throws(() => canvasPoint({ clientX: 0, clientY: 0 }, { left: 0, top: 0, zoom: NaN }), /CANVAS_ZOOM/)
})

test('畫布邊界涵蓋長邊與標籤，包含既存負座標圖的繞行', () => {
  const manual = {
    nodes: [{ ...node('a'), position: { x: -30, y: 24 } }, { ...node('obstacle'), position: { x: -30, y: 232 } }, { ...node('b'), position: { x: -30, y: 440 } }],
    edges: [edge('a', 'b'), edge('b', 'a')],
  }
  for (const graph of [layoutGraph(diamond()), manual]) {
    const bounds = graphBounds(graph)
    for (const item of graph.edges) {
      const route = edgeGeometry(graph, item)
      for (const point of route.points) {
        assert.ok(point.x >= bounds.x && point.x <= bounds.x + bounds.width)
        assert.ok(point.y >= bounds.y && point.y <= bounds.y + bounds.height)
      }
      if (item.branch) {
        assert.ok(route.label.x - 32 >= bounds.x)
        assert.ok(route.label.x + 32 <= bounds.x + bounds.width)
        assert.ok(route.label.y + 4 <= bounds.y + bounds.height)
      }
    }
  }
})

function assertLabelsClear(graph, routes) {
  graph.edges.forEach((item, index) => {
    if (!item.branch) return
    const route = routes[index]
    assert.ok(route)
    const halfWidth = item.branch === 'false' ? 32 : 24
    for (const target of graph.nodes) {
      assert.ok(route.label.x + halfWidth <= target.position.x || route.label.x - halfWidth >= target.position.x + 200 ||
        route.label.y + 13 <= target.position.y || route.label.y - 13 >= target.position.y + 152,
      `${item.source}→${item.target} 標籤不重疊 ${target.id}`)
    }
  })
}

test('32px近距離出口標籤不再被下一節點遮住，反向及負座標亦避開全部外框', () => {
  for (const [source, target] of [
    [{ x: 0, y: 0 }, { x: 0, y: 184 }],
    [{ x: 0, y: 184 }, { x: 0, y: 0 }],
    [{ x: -340, y: -300 }, { x: -340, y: -116 }],
  ]) {
    const graph = { nodes: [{ ...node('a', 'condition'), position: source }, { ...node('b'), position: target }], edges: [edge('a', 'b', 'true')] }
    const routes = graph.edges.map(item => edgeGeometry(graph, item))
    assertLabelsClear(graph, routes)
    assertRouteClear(graph, routes[0])
    assert.ok(Math.abs(routes[0].label.y - routes[0].start.y) <= 24, '標籤保留在對應出口附近')
  }
})

test('近距離雙分支採有界較小clearance，不繞回來源上方且兩線不交叉', () => {
  const graph = {
    nodes: [{ ...node('a', 'condition'), position: { x: 0, y: 0 } }, { ...node('b'), position: { x: 0, y: 184 } }, { ...node('c'), position: { x: 220, y: 170 } }],
    edges: [edge('a', 'b', 'true'), edge('a', 'c', 'false')],
  }
  const routes = graph.edges.map(item => edgeGeometry(graph, item))
  assertLabelsClear(graph, routes)
  routes.forEach(route => {
    assertRouteClear(graph, route)
    assert.ok(route.points.every(point => point.y >= 152), '無不必要的上方迴繞')
  })
  for (let i = 1; i < routes[0].points.length; i++) for (let j = 1; j < routes[1].points.length; j++) {
    const a = routes[0].points[i - 1], b = routes[0].points[i]
    const c = routes[1].points[j - 1], d = routes[1].points[j]
    if ((a.x === b.x) === (c.x === d.x)) continue
    const vertical = a.x === b.x ? [a, b] : [c, d]
    const horizontal = a.x === b.x ? [c, d] : [a, b]
    assert.ok(vertical[0].x < Math.min(horizontal[0].x, horizontal[1].x) || vertical[0].x > Math.max(horizontal[0].x, horizontal[1].x) ||
      horizontal[0].y < Math.min(vertical[0].y, vertical[1].y) || horizontal[0].y > Math.max(vertical[0].y, vertical[1].y), '成立與不成立線不交叉')
  }
})

test('bounds可重用既算路徑且fit排除遙遠正座標原點空白，包含真26px標籤外框', () => {
  const graph = { nodes: [{ ...node('a', 'condition'), position: { x: 10000, y: 10000 } }, { ...node('b'), position: { x: 10000, y: 10184 } }], edges: [edge('a', 'b', 'false')] }
  const routes = graph.edges.map(item => edgeGeometry(graph, item))
  assert.deepEqual(graphBounds(graph), graphBounds(graph, {}, routes))
  const content = graphBounds(graph, { includeOrigin: false }, routes)
  assert.equal(content.x, 9976)
  assert.equal(content.y, 9976)
  assert.equal(content.width, 248)
  assert.equal(content.height, 384)
  assert.ok(graphBounds(graph).width > 10000)
  const label = routes[0].label
  assert.ok(label.y + 13 <= content.y + content.height)
  // A supplied array is authoritative; no hidden reroute or edge endpoint lookup.
  const noLookup = { ...graph, edges: [{ source: 'missing', target: 'missing', branch: 'false' }] }
  assert.deepEqual(graphBounds(noLookup, { includeOrigin: false }, routes), content)
})

test('直角快速路徑仍保留正交轉角，不因重複waypoint產生斜線', () => {
  const graph = { nodes: [{ ...node('a', 'condition'), position: { x: 0, y: 0 } }, { ...node('b'), position: { x: 300, y: 176 } }], edges: [edge('a', 'b', 'true')] }
  const route = edgeGeometry(graph, graph.edges[0])
  assertRouteClear(graph, route)
  assertLabelsClear(graph, [route])
})

test('80節點157合法分支的原始與整理圖皆可路由，重用幾何不重算bounds', () => {
  const id = index => `n${String(index).padStart(2, '0')}`
  const original = {
    nodes: Array.from({ length: 80 }, (_, index) => ({ ...node(id(index), index === 0 ? 'input' : index === 79 ? 'output' : 'condition'), position: { x: index * 233, y: index * 211 } })),
    edges: [edge(id(0), id(1))],
  }
  for (let index = 1; index < 79; index++) original.edges.push(edge(id(index), id(index + 1), 'true'), edge(id(index), id(Math.min(79, index + 40)), 'false'))
  assert.equal(original.edges.length, 157)
  for (const graph of [original, layoutGraph(original)]) {
    const routes = graph.edges.map(item => edgeGeometry(graph, item))
    assert.ok(routes.every(Boolean), '全部連線皆有安全路徑')
    routes.forEach(route => assertRouteClear(graph, route))
    assertLabelsClear(graph, routes)
    assert.deepEqual(graphBounds(graph, {}, routes), graphBounds(graph))
  }
})
