import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import {
  AnimatePresence,
  motion,
} from 'motion/react'

import {
  ArrowLeft,
  Check,
  ChevronRight,
  Circle,
  Crosshair,
  Home,
  Map as MapIcon,
  Maximize2,
  Network,
  Pencil,
  Pin,
  PinOff,
  Play,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'

import useTaskTree, {
  TASK_NODE_TYPES,
} from './useTaskTree'

const SPACE_DOTS = [
  { x: '18%', y: '20%', z: -120, scale: 0.62 },
  { x: '77%', y: '18%', z: -40, scale: 0.84 },
  { x: '62%', y: '32%', z: 90, scale: 0.52 },
  { x: '28%', y: '64%', z: 40, scale: 0.74 },
  { x: '84%', y: '70%', z: -90, scale: 0.58 },
  { x: '12%', y: '76%', z: 80, scale: 0.48 },
  { x: '48%', y: '15%', z: 120, scale: 0.44 },
  { x: '51%', y: '84%', z: -30, scale: 0.68 },
]

const PINNED_SHORTCUTS_KEY =
  'jarvis_tree_pinned_shortcuts_v1'

const TONE_PRESETS = [
  [126, 219, 255],
  [166, 229, 196],
  [255, 210, 132],
  [227, 182, 255],
]

const CAROUSEL_SPACING = 430
const CAROUSEL_WHEEL_LOCK_MS = 140
const CAROUSEL_MOVE_SECONDS = 0.22

const getTone = (nodeId) => {
  const seed = String(nodeId || '')
    .split('')
    .reduce(
      (sum, character) =>
        sum + character.charCodeAt(0),
      0,
    )

  return TONE_PRESETS[
    seed % TONE_PRESETS.length
  ]
}

const readPinnedShortcuts = () => {
  if (typeof window === 'undefined') {
    return []
  }

  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(
        PINNED_SHORTCUTS_KEY,
      ) || '[]',
    )

    return Array.isArray(parsed)
      ? parsed.filter(
          (nodeId) =>
            typeof nodeId === 'string',
        )
      : []
  } catch (error) {
    console.warn(
      'Failed to load JARVIS pinned shortcuts.',
      error,
    )

    return []
  }
}

const createDraft = (node) => ({
  nodeId: node.id,
  label: node.label,
  description: node.description,
  type: node.type,
})

const createAddDraft = () => ({
  label: '',
  description: '',
  type: 'task',
})

const getCompletionPercent = (
  stats,
) => {
  if (!stats.total) {
    return 0
  }

  return Math.round(
    (stats.completed / stats.total) *
      100,
  )
}

const flattenTree = (
  node,
  depth = 0,
  parentIds = [],
) => [
  {
    node,
    depth,
    parentIds,
  },
  ...node.children.flatMap(
    (child) =>
      flattenTree(
        child,
        depth + 1,
        [
          ...parentIds,
          node.id,
        ],
      ),
  ),
]

export default function TreePrototype({
  onOpenExecution,
}) {
  const taskTree =
    useTaskTree()

  const popoverRef =
    useRef(null)

  const lastCarouselWheelAtRef =
    useRef(0)

  const [
    currentNodeId,
    setCurrentNodeId,
  ] = useState(
    taskTree.root.id,
  )

  const [
    activePopover,
    setActivePopover,
  ] = useState(null)

  const [
    dialog,
    setDialog,
  ] = useState(null)

  const [
    searchText,
    setSearchText,
  ] = useState('')

  const [
    pinnedNodeIds,
    setPinnedNodeIds,
  ] = useState(
    readPinnedShortcuts,
  )

  const [
    previewNodeId,
    setPreviewNodeId,
  ] = useState(null)

  const [
    focusIndex,
    setFocusIndex,
  ] = useState(0)

  const current = useMemo(
    () =>
      taskTree.find(
        currentNodeId,
      ) ?? {
        node: taskTree.root,
        path: [
          taskTree.root,
        ],
      },
    [
      currentNodeId,
      taskTree,
    ],
  )

  const {
    node,
    path,
  } = current

  const parent =
    path.length > 1
      ? path[path.length - 2]
      : null

  const children =
    node.children ?? []

  const focusedIndex =
    children.length
      ? (focusIndex %
          children.length +
          children.length) %
        children.length
      : 0

  const depth =
    path.length - 1

  const previewNode =
    children.find(
      (child) =>
        child.id === previewNodeId,
    ) ||
    children[focusedIndex]

  const goToNode = (
    nodeId,
  ) => {
    setCurrentNodeId(nodeId)
    setFocusIndex(0)
    setPreviewNodeId(null)
    setActivePopover(null)
  }

  const moveFocus = (
    direction,
  ) => {
    if (children.length <= 1) {
      return
    }

    setFocusIndex(
      (currentIndex) =>
        currentIndex + direction,
    )
  }

  const flatNodes = useMemo(
    () =>
      flattenTree(
        taskTree.root,
      ),
    [taskTree.root],
  )

  const flatNodeMap = useMemo(
    () =>
      new Map(
        flatNodes.map((entry) => [
          entry.node.id,
          entry,
        ]),
      ),
    [flatNodes],
  )

  const pinnedNodes =
    pinnedNodeIds
      .map((nodeId) =>
        flatNodeMap.get(nodeId),
      )
      .filter(Boolean)

  const isCurrentPinned =
    pinnedNodeIds.includes(
      node.id,
    )

  useEffect(() => {
    try {
      window.localStorage.setItem(
        PINNED_SHORTCUTS_KEY,
        JSON.stringify(
          pinnedNodeIds,
        ),
      )
    } catch (error) {
      console.warn(
        'Failed to save JARVIS pinned shortcuts.',
        error,
      )
    }
  }, [pinnedNodeIds])

  const visibleMapNodes =
    flatNodes.filter((entry) => {
      const query =
        searchText
          .trim()
          .toLowerCase()

      if (!query) {
        return true
      }

      return (
        entry.node.label
          .toLowerCase()
          .includes(query) ||
        entry.node.description
          .toLowerCase()
          .includes(query)
      )
    })

  const [
    editDraftState,
    setEditDraft,
  ] = useState(() =>
    createDraft(node),
  )

  const editDraft =
    editDraftState.nodeId ===
    node.id
      ? editDraftState
      : createDraft(node)

  const [
    addDraft,
    setAddDraft,
  ] = useState(
    createAddDraft,
  )

  useEffect(() => {
    if (!activePopover) {
      return undefined
    }

    const handlePointerDown =
      (event) => {
        if (
          popoverRef.current
            ?.contains(event.target)
        ) {
          return
        }

        setActivePopover(null)
      }

    window.addEventListener(
      'pointerdown',
      handlePointerDown,
    )

    return () => {
      window.removeEventListener(
        'pointerdown',
        handlePointerDown,
      )
    }
  }, [activePopover])

  const updateEditDraft = (
    patch,
  ) => {
    setEditDraft(
      (draft) => ({
        ...(draft.nodeId ===
        node.id
          ? draft
          : createDraft(node)),
        ...patch,
      }),
    )
  }

  const saveCurrentNode = (
    event,
  ) => {
    event.preventDefault()

    taskTree.updateNode(
      node.id,
      editDraft,
    )

    setActivePopover(null)
  }

  const addChild = (event) => {
    event.preventDefault()

    const nextNodeId =
      taskTree.addChild(
        node.id,
        addDraft,
      )

    if (!nextNodeId) {
      return
    }

    setAddDraft(
      createAddDraft(),
    )

    goToNode(nextNodeId)
  }

  const deleteCurrentNode = () => {
    const nextNodeId =
      taskTree.deleteNode(
        node.id,
      )

    goToNode(nextNodeId)
    setDialog(null)
  }

  const requestDeleteCurrentNode =
    () => {
      if (
        node.id ===
        taskTree.root.id
      ) {
        return
      }

      setDialog('delete')
      setActivePopover(null)
    }

  const toggleCurrentPin = () => {
    setPinnedNodeIds(
      (nodeIds) => {
        if (
          nodeIds.includes(
            node.id,
          )
        ) {
          return nodeIds.filter(
            (nodeId) =>
              nodeId !== node.id,
          )
        }

        return [
          node.id,
          ...nodeIds,
        ].slice(0, 6)
      },
    )
  }

  const executeCurrentNode = () => {
    onOpenExecution({
      node,
      path,
    })
  }

  const handleCarouselWheel = (
    event,
  ) => {
    if (children.length <= 1) {
      return
    }

    const delta =
      Math.abs(event.deltaX) >
      Math.abs(event.deltaY)
        ? event.deltaX
        : event.deltaY

    if (Math.abs(delta) < 8) {
      return
    }

    const now =
      window.performance.now()

    if (
      now -
        lastCarouselWheelAtRef.current <
      CAROUSEL_WHEEL_LOCK_MS
    ) {
      return
    }

    event.preventDefault()

    lastCarouselWheelAtRef.current =
      now

    moveFocus(
      delta > 0 ? 1 : -1,
    )
  }

  const updateSpotlight = (
    event,
  ) => {
    const rect =
      event.currentTarget
        .getBoundingClientRect()

    event.currentTarget
      .style.setProperty(
        '--tree-spotlight-x',
        `${event.clientX - rect.left}px`,
      )

    event.currentTarget
      .style.setProperty(
        '--tree-spotlight-y',
        `${event.clientY - rect.top}px`,
      )

    event.currentTarget
      .style.setProperty(
        '--tree-spotlight-opacity',
        '1',
      )
  }

  const hideSpotlight = (
    event,
  ) => {
    event.currentTarget
      .style.setProperty(
        '--tree-spotlight-opacity',
        '0',
      )
  }

  const focusNode =
    previewNode || node

  const [
    toneRed,
    toneGreen,
    toneBlue,
  ] = getTone(focusNode.id)

  const carouselItems =
    children.map(
      (child, childIndex) => {
        let slot =
          childIndex - focusedIndex

        if (
          children.length > 2 &&
          slot > children.length / 2
        ) {
          slot -= children.length
        }

        if (
          children.length > 2 &&
          slot <
            -children.length / 2
        ) {
          slot += children.length
        }

        return {
          slot,
          child,
          childIndex,
        }
      },
    )

  return (
    <section
      className="tree-prototype-interface"
      aria-label="JARVIS System Home"
      data-depth={depth}
      data-preview-active={
        previewNode ? 'true' : 'false'
      }
      onPointerMove={updateSpotlight}
      onPointerLeave={hideSpotlight}
      style={{
        '--tree-tone-r':
          toneRed,
        '--tree-tone-g':
          toneGreen,
        '--tree-tone-b':
          toneBlue,
      }}
    >
      <div
        className="tree-prototype-cursor-spotlight"
        aria-hidden="true"
      />

      <div
        className="tree-prototype-field"
        aria-hidden="true"
      />

      <div
        className="tree-prototype-space"
        aria-hidden="true"
      >
        {SPACE_DOTS.map(
          (dot, index) => (
            <span
              key={`${dot.x}-${dot.y}`}
              style={{
                '--space-x': dot.x,
                '--space-y': dot.y,
                '--space-z':
                  `${dot.z}px`,
                '--space-scale':
                  dot.scale,
                '--space-delay':
                  `${index * 0.37}s`,
              }}
            />
          ),
        )}
      </div>

      <div
        className="tree-prototype-depth-field"
        aria-hidden="true"
      >
        {path.map(
          (pathNode, index) => (
            <span
              key={pathNode.id}
              className="tree-prototype-depth-ring"
              style={{
                '--depth-index':
                  index,
              }}
            />
          ),
        )}
      </div>

      <header className="tree-prototype-context">
        <div className="tree-prototype-kicker">
          <Network
            size={13}
            strokeWidth={1.7}
            aria-hidden="true"
          />
          SYSTEM HOME
        </div>

        <div className="tree-prototype-breadcrumb">
          {path.map(
            (pathNode, index) => (
              <span
                key={pathNode.id}
                className="tree-prototype-breadcrumb-part"
              >
                {index > 0 && (
                  <ChevronRight
                    size={11}
                    aria-hidden="true"
                  />
                )}

                <button
                  type="button"
                  onClick={() =>
                    goToNode(
                      pathNode.id,
                    )
                  }
                >
                  {pathNode.label}
                </button>
              </span>
            ),
          )}
        </div>
      </header>

      {parent && (
        <motion.button
          type="button"
          className="tree-prototype-parent"
          whileTap={{
            scale: 0.97,
          }}
          onClick={() =>
            goToNode(
              parent.id,
            )
          }
        >
          <ArrowLeft
            size={14}
            aria-hidden="true"
          />
          {parent.label}
        </motion.button>
      )}

      <motion.div
        key={node.id}
        className="tree-prototype-current"
        initial={false}
        animate={{
          opacity: 1,
          y: 0,
          scale: 1,
        }}
        transition={{
          duration: 0.03,
          ease: [
            0.22,
            1,
            0.36,
            1,
          ],
        }}
      >
        <div
          className="tree-prototype-core-anchor"
          aria-hidden="true"
        >
          <span />
          <span />
          <span />
        </div>

        <span className="tree-prototype-current-silent-label">
          {node.label}
        </span>
      </motion.div>

      <div
        key={`${node.id}-children`}
        className={[
          'tree-prototype-children',
          'tree-prototype-carousel',
          children.length === 0
            ? 'is-empty'
            : '',
        ]
          .filter(Boolean)
          .join(' ')}
        onWheel={
          handleCarouselWheel
        }
      >
        <div
          className="tree-carousel-progressive-blur tree-carousel-progressive-blur-left"
          aria-hidden="true"
        />

        <div
          className="tree-carousel-progressive-blur tree-carousel-progressive-blur-right"
          aria-hidden="true"
        />

        {children.length > 1 && (
          <>
          <div
  className="tree-carousel-ghost-layer"
  aria-hidden="true"
>
  {[
    {
      id: 'left',
      x: -400,
      y: -120,
      z: -360,
      scale: 0.82,
      opacity: 0.04,
      rotateY: 12,
    },
    {
      id: 'center',
      x: 0,
      y: -175,
      z: -320,
      scale: 0.78,
      opacity: 0.06,
      rotateY: 0,
    },
    {
      id: 'right',
      x: 400,
      y: -120,
      z: -360,
      scale: 0.82,
      opacity: 0.04,
      rotateY: -12,
    },
  ].map((ghost) => (
    <motion.div
      key={ghost.id}
      className="tree-carousel-ghost-card"
      initial={false}
      animate={{
        x: ghost.x,
        y: ghost.y,
        z: ghost.z,
        scale: ghost.scale,
        opacity: ghost.opacity,
        rotateX: 0,
        rotateY: ghost.rotateY,
        rotateZ: 0,
      }}
      transition={{
        duration:
          CAROUSEL_MOVE_SECONDS,
        ease: [
          0.22,
          1,
          0.36,
          1,
        ],
      }}
    />
  ))}
</div>
            <button
              type="button"
              className="tree-carousel-arrow tree-carousel-arrow-left"
              onClick={() =>
                moveFocus(-1)
              }
              aria-label="Previous node"
            >
              &lt;
            </button>

            <button
              type="button"
              className="tree-carousel-arrow tree-carousel-arrow-right"
              onClick={() =>
                moveFocus(1)
              }
              aria-label="Next node"
            >
              &gt;
            </button>
          </>
        )}

        {carouselItems.map(
          ({
            child,
            childIndex,
            slot,
          }) => {
            const childStats =
              taskTree.stats(child)

            const distance =
              Math.abs(slot)

            /*
              Shallow 3D elliptical carousel.

              - focused card: Core 바로 아래 전면
              - side cards: Core 양옆/뒤로 살짝 올라감
              - far cards: 뒤쪽을 돌아가는 희미한 잔상
              - idle 상태에서는 정지
              - focus가 바뀔 때만 기존 0.22s lateral motion으로 이동
            */
            const isFocusedNode =
              slot === 0

            const isSideNode =
              distance === 1

            const isGhostNode =
              distance > 1

            const direction =
              Math.sign(slot)

            const carouselX =
              isFocusedNode
                ? 0
                : direction *
                  (isSideNode
                    ? 360
                    : 650 +
                      Math.max(
                        0,
                        distance - 2,
                      ) *
                        150)

            const carouselY =
              isFocusedNode
                ? 130
                : isSideNode
                  ? 100
                  : 30

            const carouselZ =
              isFocusedNode
                ? 55
                : isSideNode
                  ? -95
                  : -250 -
                    Math.max(
                      0,
                      distance - 2,
                    ) *
                      90

            const carouselScale =
              isFocusedNode
                ? 1
                : isSideNode
                  ? 0.90
                  : 0.76

            const carouselOpacity =
              isFocusedNode
                ? 1
                : isSideNode
                  ? 0.64
                  : Math.max(
                      0.08,
                      0.18 -
                        Math.max(
                          0,
                          distance - 2,
                        ) *
                          0.05,
                    )

            const carouselRotateY =
              isFocusedNode
                ? 0
                : -direction *
                  (isSideNode
                    ? 12
                    : 28)

            const carouselRotateX =
              isFocusedNode
                ? 0
                : isSideNode
                  ? 2
                  : 7

            const carouselFilter =
              isFocusedNode
                ? 'blur(0px) brightness(1)'
                : isSideNode
                  ? 'blur(0px) brightness(0.94)'
                  : 'blur(1.4px) brightness(0.68)'

            return (
              <motion.button
                type="button"
                key={child.id}
                className={[
                  'tree-prototype-node',
                  child.complete
                    ? 'is-complete'
                    : '',
                  isFocusedNode
                    ? 'is-focused-carousel'
                    : '',
                  isSideNode
                    ? 'is-side-carousel'
                    : '',
                  isGhostNode
                    ? 'is-far-carousel'
                    : '',
                  `tree-prototype-node-${
                    childIndex % 4
                  }`,
                ]
                  .filter(Boolean)
                  .join(' ')}
                initial={false}
                style={{
                  zIndex:
                    10 - distance,
                  pointerEvents:
                    isGhostNode
                      ? 'none'
                      : 'auto',
                }}
                animate={{
                  opacity:
                    carouselOpacity,
                  x: carouselX,
                  y: carouselY,
                  z: carouselZ,
                  rotateX:
                    carouselRotateX,
                  rotateY:
                    carouselRotateY,
                  rotateZ: 0,
                  scale:
                    carouselScale,
                  filter:
                    carouselFilter,
                }}
                transition={{
                  duration:
                    CAROUSEL_MOVE_SECONDS,
                  ease: [
                    0.22,
                    1,
                    0.36,
                    1,
                  ],
                }}
                whileHover={{
                  scale:
                    carouselScale *
                    1.025,
                  z:
                    carouselZ + 18,
                  opacity: 1,
                  filter:
                    'blur(0px) brightness(1.04)',
                }}
                whileTap={{
                  scale: 0.985,
                }}
                onClick={() => {
                  if (
                    isFocusedNode
                  ) {
                    goToNode(
                      child.id,
                    )
                    return
                  }

                  setFocusIndex(
                    (currentIndex) =>
                      currentIndex +
                      slot,
                  )
                }}
                onPointerEnter={() =>
                  setPreviewNodeId(
                    child.id,
                  )
                }
                onPointerLeave={() =>
                  setPreviewNodeId(
                    null,
                  )
                }
              >
                <span
                  className="tree-prototype-node-energy"
                  aria-hidden="true"
                />

                <span className="tree-prototype-node-eyebrow">
                  {child.eyebrow}
                </span>

                <strong>
                  {child.label}
                </strong>

                <span className="tree-prototype-node-description">
                  {child.description ||
                    'No description yet.'}
                </span>

                <span className="tree-prototype-node-meta">
                  <span>
                    {getCompletionPercent(
                      childStats,
                    )}
                    %
                  </span>
                  <span>
                    {
                      child.children
                        .length
                    }{' '}
                    paths
                  </span>
                </span>

                <span className="tree-prototype-node-enter">
                  {child.children
                    ?.length
                    ? `${child.children.length} PATHS`
                    : 'OPEN'}
                  <ChevronRight
                    size={13}
                    aria-hidden="true"
                  />
                </span>
              </motion.button>
            )
          },
        )}

        {children.length === 0 && (
          <div className="tree-prototype-leaf">
            <span>
              EXECUTION READY
            </span>
            <strong>
              Use the dock to add a child or open this node for execution.
            </strong>
          </div>
        )}
      </div>

      <aside className="tree-prototype-overview">
        <div className="tree-prototype-overview-title">
          <MapIcon
            size={12}
            strokeWidth={1.7}
            aria-hidden="true"
          />
          SYSTEM MAP

          <button
            type="button"
            className="tree-prototype-overview-expand"
            onClick={() =>
              setDialog('map')
            }
            aria-label="Open expanded system map"
          >
            <Maximize2
              size={11}
              aria-hidden="true"
            />
          </button>
        </div>

        <div className="tree-prototype-overview-search">
          <Search
            size={11}
            aria-hidden="true"
          />
          <input
            type="search"
            value={searchText}
            placeholder="Find node"
            onChange={(event) =>
              setSearchText(
                event.target.value,
              )
            }
            aria-label="Search tree nodes"
          />
        </div>

        <div className="tree-prototype-overview-list">
          {visibleMapNodes.map(
            (entry) => (
              <button
                type="button"
                key={
                  entry.node.id
                }
                className={[
                  entry.node.id ===
                  node.id
                    ? 'is-active'
                    : '',
                  entry.parentIds.includes(
                    node.id,
                  )
                    ? 'is-descendant'
                    : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{
                  '--map-depth':
                    entry.depth,
                }}
                onClick={() =>
                  goToNode(
                    entry.node.id,
                  )
                }
              >
                <span />
                {
                  entry.node
                    .label
                }
              </button>
            ),
          )}
        </div>
      </aside>

      <nav
        className="tree-prototype-dock"
        aria-label="JARVIS tree actions"
        ref={popoverRef}
      >
        <button
          type="button"
          aria-label="Go to system home"
          onClick={() =>
            goToNode(
              taskTree.root.id,
            )
          }
        >
          <Home
            size={16}
            aria-hidden="true"
          />
          <span>Home</span>
        </button>

        <button
          type="button"
          aria-label="Go to parent"
          disabled={!parent}
          onClick={() =>
            parent &&
            goToNode(
              parent.id,
            )
          }
        >
          <ArrowLeft
            size={16}
            aria-hidden="true"
          />
          <span>Back</span>
        </button>

        <button
          type="button"
          aria-label="Edit current node"
          className={
            activePopover === 'edit'
              ? 'is-active'
              : ''
          }
          onClick={(event) => {
            event.stopPropagation()

            setActivePopover(
              activePopover === 'edit'
                ? null
                : 'edit',
            )
          }}
        >
          <Pencil
            size={16}
            aria-hidden="true"
          />
          <span>Edit</span>
        </button>

        <button
          type="button"
          aria-label="Add child node"
          className={
            activePopover === 'add'
              ? 'is-active'
              : ''
          }
          onClick={(event) => {
            event.stopPropagation()

            setActivePopover(
              activePopover === 'add'
                ? null
                : 'add',
            )
          }}
        >
          <Plus
            size={16}
            aria-hidden="true"
          />
          <span>Add</span>
        </button>

        <button
          type="button"
          aria-label={
            isCurrentPinned
              ? 'Unpin current node'
              : 'Pin current node'
          }
          className={
            isCurrentPinned
              ? 'is-active'
              : ''
          }
          onClick={
            toggleCurrentPin
          }
        >
          {isCurrentPinned ? (
            <PinOff
              size={16}
              aria-hidden="true"
            />
          ) : (
            <Pin
              size={16}
              aria-hidden="true"
            />
          )}

          <span>
            {isCurrentPinned
              ? 'Unpin'
              : 'Pin'}
          </span>
        </button>

        <button
          type="button"
          aria-label="Search system map"
          className={
            activePopover ===
            'search'
              ? 'is-active'
              : ''
          }
          onClick={(event) => {
            event.stopPropagation()

            setActivePopover(
              activePopover ===
              'search'
                ? null
                : 'search',
            )
          }}
        >
          <Search
            size={16}
            aria-hidden="true"
          />
          <span>Search</span>
        </button>

        <button
          type="button"
          aria-label="Open execution"
          onClick={
            executeCurrentNode
          }
        >
          <Play
            size={16}
            fill="currentColor"
            aria-hidden="true"
          />
          <span>Run</span>
        </button>

        {pinnedNodes.map(
          (entry) => (
            <button
              type="button"
              key={entry.node.id}
              aria-label={`Open ${entry.node.label}`}
              className={
                path.some(
                  (pathNode) =>
                    pathNode.id ===
                    entry.node.id,
                )
                  ? 'is-active'
                  : ''
              }
              onClick={() =>
                goToNode(
                  entry.node.id,
                )
              }
            >
              <Crosshair
                size={16}
                aria-hidden="true"
              />
              <span>
                {
                  entry.node
                    .label
                }
              </span>
            </button>
          ),
        )}

        <AnimatePresence>
          {activePopover ===
            'search' && (
            <motion.div
              key="search-popover"
              className="tree-prototype-popover tree-prototype-dock-search"
              initial={{
                opacity: 0,
                y: 12,
                scale: 0.94,
              }}
              animate={{
                opacity: 1,
                y: 0,
                scale: 1,
              }}
              exit={{
                opacity: 0,
                y: 10,
                scale: 0.96,
              }}
              transition={{
                type: 'spring',
                bounce: 0.08,
                duration: 0.18,
              }}
              onPointerDown={(
                event,
              ) =>
                event.stopPropagation()
              }
            >
              <Search
                size={13}
                aria-hidden="true"
              />
              <input
                type="search"
                value={
                  searchText
                }
                placeholder="Search node"
                onChange={(
                  event,
                ) =>
                  setSearchText(
                    event.target
                      .value,
                  )
                }
                aria-label="Search tree nodes from dock"
                autoFocus
              />
            </motion.div>
          )}

          {activePopover ===
            'edit' && (
            <motion.form
              key="edit-popover"
              className="tree-prototype-popover tree-prototype-edit-form"
              initial={{
                opacity: 0,
                y: 12,
                scale: 0.94,
              }}
              animate={{
                opacity: 1,
                y: 0,
                scale: 1,
              }}
              exit={{
                opacity: 0,
                y: 10,
                scale: 0.96,
              }}
              transition={{
                type: 'spring',
                bounce: 0.08,
                duration: 0.03,
              }}
              onSubmit={
                saveCurrentNode
              }
            >
              <div className="tree-prototype-editor-title">
                <Pencil
                  size={12}
                  strokeWidth={1.8}
                  aria-hidden="true"
                />
                CURRENT NODE
              </div>

              <input
                type="text"
                value={
                  editDraft.label
                }
                onChange={(
                  event,
                ) =>
                  updateEditDraft({
                    label:
                      event.target
                        .value,
                  })
                }
                aria-label="Current node title"
              />

              <textarea
                value={
                  editDraft.description
                }
                onChange={(
                  event,
                ) =>
                  updateEditDraft({
                    description:
                      event.target
                        .value,
                  })
                }
                aria-label="Current node description"
              />

              <select
                value={
                  editDraft.type
                }
                onChange={(
                  event,
                ) =>
                  updateEditDraft({
                    type:
                      event.target
                        .value,
                  })
                }
                aria-label="Current node type"
              >
                {TASK_NODE_TYPES.map(
                  (type) => (
                    <option
                      key={type}
                      value={type}
                    >
                      {type}
                    </option>
                  ),
                )}
              </select>

              <div className="tree-prototype-editor-actions">
                <button type="submit">
                  SAVE
                </button>

                <button
                  type="button"
                  onClick={() =>
                    taskTree.toggleComplete(
                      node.id,
                    )
                  }
                >
                  {node.complete ? (
                    <Check
                      size={12}
                      aria-hidden="true"
                    />
                  ) : (
                    <Circle
                      size={12}
                      aria-hidden="true"
                    />
                  )}

                  {node.complete
                    ? 'DONE'
                    : 'MARK'}
                </button>

                <button
                  type="button"
                  onClick={
                    requestDeleteCurrentNode
                  }
                  disabled={
                    node.id ===
                    taskTree.root.id
                  }
                >
                  <Trash2
                    size={12}
                    aria-hidden="true"
                  />
                  DELETE
                </button>
              </div>
            </motion.form>
          )}

          {activePopover ===
            'add' && (
            <motion.form
              key="add-popover"
              className="tree-prototype-popover tree-prototype-add-form"
              initial={{
                opacity: 0,
                y: 12,
                scale: 0.94,
              }}
              animate={{
                opacity: 1,
                y: 0,
                scale: 1,
              }}
              exit={{
                opacity: 0,
                y: 10,
                scale: 0.96,
              }}
              transition={{
                type: 'spring',
                bounce: 0.08,
                duration: 0.03,
              }}
              onSubmit={addChild}
            >
              <div className="tree-prototype-editor-title">
                <Plus
                  size={12}
                  strokeWidth={1.8}
                  aria-hidden="true"
                />
                ADD CHILD
              </div>

              <input
                type="text"
                value={
                  addDraft.label
                }
                placeholder="New task or sub-goal"
                onChange={(
                  event,
                ) =>
                  setAddDraft(
                    (draft) => ({
                      ...draft,
                      label:
                        event.target
                          .value,
                    }),
                  )
                }
                aria-label="New child title"
              />

              <textarea
                value={
                  addDraft.description
                }
                placeholder="Why this node matters"
                onChange={(
                  event,
                ) =>
                  setAddDraft(
                    (draft) => ({
                      ...draft,
                      description:
                        event.target
                          .value,
                    }),
                  )
                }
                aria-label="New child description"
              />

              <div className="tree-prototype-add-row">
                <select
                  value={
                    addDraft.type
                  }
                  onChange={(
                    event,
                  ) =>
                    setAddDraft(
                      (draft) => ({
                        ...draft,
                        type:
                          event.target
                            .value,
                      }),
                    )
                  }
                  aria-label="New child type"
                >
                  {TASK_NODE_TYPES.map(
                    (type) => (
                      <option
                        key={
                          type
                        }
                        value={
                          type
                        }
                      >
                        {type}
                      </option>
                    ),
                  )}
                </select>

                <button type="submit">
                  ADD
                </button>
              </div>
            </motion.form>
          )}
        </AnimatePresence>
      </nav>

      <AnimatePresence>
        {dialog === 'delete' && (
          <motion.div
            className="tree-prototype-dialog-backdrop"
            initial={{
              opacity: 0,
            }}
            animate={{
              opacity: 1,
            }}
            exit={{
              opacity: 0,
            }}
          >
            <motion.div
              className="tree-prototype-dialog"
              initial={{
                opacity: 0,
                y: 18,
                scale: 0.96,
              }}
              animate={{
                opacity: 1,
                y: 0,
                scale: 1,
              }}
              exit={{
                opacity: 0,
                y: 10,
                scale: 0.97,
              }}
            >
              <div className="tree-prototype-dialog-title">
                Delete node
              </div>

              <p>
                {node.label} and its child paths will be removed.
              </p>

              <div className="tree-prototype-dialog-actions">
                <button
                  type="button"
                  onClick={() =>
                    setDialog(null)
                  }
                >
                  CANCEL
                </button>

                <button
                  type="button"
                  className="is-danger"
                  onClick={
                    deleteCurrentNode
                  }
                >
                  DELETE
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}

        {dialog === 'map' && (
          <motion.div
            className="tree-prototype-dialog-backdrop"
            initial={{
              opacity: 0,
            }}
            animate={{
              opacity: 1,
            }}
            exit={{
              opacity: 0,
            }}
          >
            <motion.div
              className="tree-prototype-dialog tree-prototype-map-dialog"
              initial={{
                opacity: 0,
                y: 18,
                scale: 0.96,
              }}
              animate={{
                opacity: 1,
                y: 0,
                scale: 1,
              }}
              exit={{
                opacity: 0,
                y: 10,
                scale: 0.97,
              }}
            >
              <div className="tree-prototype-map-dialog-header">
                <div className="tree-prototype-dialog-title">
                  System map
                </div>

                <button
                  type="button"
                  onClick={() =>
                    setDialog(null)
                  }
                  aria-label="Close expanded system map"
                >
                  <X
                    size={14}
                    aria-hidden="true"
                  />
                </button>
              </div>

              <div className="tree-prototype-map-dialog-list">
                {visibleMapNodes.map(
                  (entry) => (
                    <button
                      type="button"
                      key={
                        entry.node.id
                      }
                      className={
                        entry.node
                          .id ===
                        node.id
                          ? 'is-active'
                          : ''
                      }
                      style={{
                        '--map-depth':
                          entry.depth,
                      }}
                      onClick={() => {
                        goToNode(
                          entry.node
                            .id,
                        )
                        setDialog(
                          null,
                        )
                      }}
                    >
                      <span />
                      {
                        entry.node
                          .label
                      }
                    </button>
                  ),
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}
