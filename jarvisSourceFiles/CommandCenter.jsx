import {
  useLayoutEffect,
  useRef,
  useState,
} from 'react'

import {
  createScope,
  createTimeline,
} from 'animejs'

import {
  motion,
} from 'motion/react'

import {
  Check,
  ChevronRight,
  Circle,
  ListChecks,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Square,
  Target,
  Timer,
  X,
  Zap,
} from 'lucide-react'

import {
  TIMER_PRESETS_MINUTES,
} from './useExecutionSession'

import SevenSegmentTime from './SevenSegmentTime'

/*
  Command Center entrance choreography.

  이 값들은 Electron PiP ↔ fullscreen 전환 시간과 분리한다.
  여기서는 "Command Center가 mount된 뒤 내부 UI가 어떻게 구성되는가"만 다룬다.

  의미 순서:
  surface  → 공간의 존재
  context  → 지금 무엇을 하고 있는가
  action   → 지금 무엇을 해야 하는가
  support  → 실행을 돕는 도구
  ambient  → 부가적인 외부-world placeholder
*/
const COMMAND_CENTER_MOTION = {
  panelDurationMs: 520,
  orbitGuideDurationMs: 760,
  ambientDurationMs: 440,

  surfaceAtMs: 0,
  contextAtMs: 90,
  actionAtMs: 150,
  timerAtMs: 245,
  checklistAtMs: 300,
  ambientAtMs: 360,
}

const MICRO_TAP = {
  scale: 0.97,
}

export default function CommandCenter({
  execution,
  executionContext,
}) {
  const rootRef = useRef(null)

  const [newItem, setNewItem] =
    useState('')

  /*
    Anime.js boundary:

    - 일회성 Command Center entrance choreography만 담당한다.
    - Core orbit / Core breathing / panel idle float는 CSS가 계속 담당한다.
    - PiP ↔ fullscreen Electron transition은 App.jsx의 기존 phase flow를 유지한다.
    - 기존 rotateX / rotateY를 덮어쓰지 않도록 CSS custom property만 움직인다.

    createScope는 이 component 내부로 selector를 제한하고,
    unmount 시 timeline을 한 번에 정리한다.
  */
  useLayoutEffect(() => {
    const scope = createScope({
      root: rootRef,
      mediaQueries: {
        reducedMotion:
          '(prefers-reduced-motion: reduce)',
      },
    }).add((self) => {
      if (
        self.matches.reducedMotion
      ) {
        return
      }

      const entrance =
        createTimeline({
          autoplay: false,
          defaults: {
            duration:
              COMMAND_CENTER_MOTION
                .panelDurationMs,
            ease: 'out(4)',
          },
        })

      entrance
        .label(
          'surface',
          COMMAND_CENTER_MOTION
            .surfaceAtMs,
        )
        .label(
          'context',
          COMMAND_CENTER_MOTION
            .contextAtMs,
        )
        .label(
          'action',
          COMMAND_CENTER_MOTION
            .actionAtMs,
        )
        .label(
          'timer',
          COMMAND_CENTER_MOTION
            .timerAtMs,
        )
        .label(
          'checklist',
          COMMAND_CENTER_MOTION
            .checklistAtMs,
        )
        .label(
          'ambient',
          COMMAND_CENTER_MOTION
            .ambientAtMs,
        )

        /*
          1. 먼저 공간의 궤도 guide가 희미하게 materialize.
        */
        .add(
          '.command-center-orbit-guide',
          {
            opacity: [0, 1],
            duration:
              COMMAND_CENTER_MOTION
                .orbitGuideDurationMs,
            ease: 'out(3)',
          },
          'surface',
        )

        /*
          2. Context: 현재 Objective.
          화면 좌측 위에서 Core 쪽으로 약하게 수렴한다.
        */
        .add(
          '.command-panel-objective',
          {
            opacity: [0, 1],
            '--panel-entry-x': [
              '-24px',
              '0px',
            ],
            '--panel-entry-y': [
              '-9px',
              '0px',
            ],
            '--panel-entry-scale': [
              0.965,
              1,
            ],
          },
          'context',
        )

        /*
          3. Action: Next Action.
          Objective보다 아주 조금 뒤에 등장시켜
          "맥락 → 실행할 것"의 읽기 순서를 만든다.
        */
        .add(
          '.command-panel-next-action',
          {
            opacity: [0, 1],
            '--panel-entry-x': [
              '-30px',
              '0px',
            ],
            '--panel-entry-y': [
              '11px',
              '0px',
            ],
            '--panel-entry-scale': [
              0.96,
              1,
            ],
          },
          'action',
        )

        /*
          4. Execution support: Timer → Checklist.
          둘은 같은 support 계층이지만 완전히 동시에 뜨지 않는다.
        */
        .add(
          '.command-panel-timer',
          {
            opacity: [0, 1],
            '--panel-entry-x': [
              '24px',
              '0px',
            ],
            '--panel-entry-y': [
              '-9px',
              '0px',
            ],
            '--panel-entry-scale': [
              0.965,
              1,
            ],
          },
          'timer',
        )
        .add(
          '.command-panel-checklist',
          {
            opacity: [0, 1],
            '--panel-entry-x': [
              '30px',
              '0px',
            ],
            '--panel-entry-y': [
              '11px',
              '0px',
            ],
            '--panel-entry-scale': [
              0.96,
              1,
            ],
          },
          'checklist',
        )

        /*
          5. External-world placeholder는 가장 늦게.
          현재 핵심 실행 정보보다 시각적 우선순위를 낮춘다.
        */
        .add(
          '.command-external-module',
          {
            opacity: [0, 1],
            duration:
              COMMAND_CENTER_MOTION
                .ambientDurationMs,
            ease: 'out(3)',
          },
          'ambient',
        )

      /*
        Timeline child의 from 값을 첫 paint 전에 모두 적용한 뒤 재생한다.
        delayed child가 잠깐 최종 위치로 보이는 flash를 피하기 위한 단계다.
      */
      entrance.init()
      entrance.play()
    })

    return () => {
      scope.revert()
    }
  }, [])

  const {
    objective,
    nextAction,
    checklist,
    timer,
    timerText,
    progress,
    actionComplete,
    currentItemId,
    statusLabel,
    actions,
  } = execution

  const selectedMinutes =
    Math.round(
      timer.originalDurationMs /
        60_000,
    )

  const durationHours =
    Math.floor(
      selectedMinutes / 60,
    )

  const durationMinutesPart =
    selectedMinutes % 60

  const addItem = (event) => {
    event.preventDefault()

    const trimmed =
      newItem.trim()

    if (!trimmed) {
      return
    }

    actions.addChecklistItem(
      trimmed,
    )

    setNewItem('')
  }

  const timerLocked =
    timer.status !== 'idle'

  const statusTone =
    timer.status === 'active'
      ? 'is-live'
      : timer.status === 'paused'
        ? 'is-paused'
        : ''

  const executionPath =
    executionContext?.path
      ?.map(
        (pathNode) =>
          pathNode.label,
      )
      .join(' / ')

  /*
    Motion Primitives의 Spotlight 아이디어를 JARVIS 구조에 맞게
    가볍게 재구현한다.

    React state를 갱신하지 않고 CSS custom property만 변경하므로
    pointer movement가 execution state / timer render와 섞이지 않는다.
  */
  const updateSurfaceLight = (event) => {
    const rect =
      event.currentTarget.getBoundingClientRect()

    event.currentTarget.style.setProperty(
      '--surface-light-x',
      `${event.clientX - rect.left}px`,
    )

    event.currentTarget.style.setProperty(
      '--surface-light-y',
      `${event.clientY - rect.top}px`,
    )

    event.currentTarget.style.setProperty(
      '--surface-light-opacity',
      '1',
    )
  }

  const hideSurfaceLight = (event) => {
    event.currentTarget.style.setProperty(
      '--surface-light-opacity',
      '0',
    )
  }

  return (
    <section
      ref={rootRef}
      className="command-center-interface antialiased"
      aria-label="JARVIS Command Center"
    >
      <div className="command-center-orbit-guide command-center-orbit-guide-a" />
      <div className="command-center-orbit-guide command-center-orbit-guide-b" />

      <div
        className="command-center-axis command-center-axis-horizontal"
        aria-hidden="true"
      />
      <div
        className="command-center-axis command-center-axis-vertical"
        aria-hidden="true"
      />

      <div className="command-workspace">
        <article
          className="command-panel command-panel-objective"
          onPointerMove={updateSurfaceLight}
          onPointerEnter={updateSurfaceLight}
          onPointerLeave={hideSurfaceLight}
        >
          <div className="command-context-left min-w-0">
            <Target
              className="command-section-icon"
              aria-hidden="true"
              size={15}
              strokeWidth={1.8}
            />

            <div>
              <div className="command-panel-eyebrow">
                Objective
              </div>

              <div className="command-objective-text">
                {executionContext?.node
                  ?.label || objective}
              </div>

              {executionPath && (
                <div className="command-objective-path">
                  {executionPath}
                </div>
              )}
            </div>
          </div>

          <div className="command-context-right shrink-0">
            <div
              className={[
                'command-status-badge',
                statusTone,
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <Zap
                size={11}
                strokeWidth={2}
                aria-hidden="true"
              />
              {statusLabel}
            </div>

            <div className="command-panel-meta">
              {executionContext?.node
                ?.type
                ? `${executionContext.node.type} layer`
                : 'Execution Layer'}
            </div>
          </div>
        </article>

        <article
          className={[
            'command-panel',
            'command-panel-next-action',
            timer.status === 'active'
              ? 'is-executing'
              : '',
            timer.status === 'paused'
              ? 'is-paused'
              : '',
          ]
            .filter(Boolean)
            .join(' ')}
          onPointerMove={updateSurfaceLight}
          onPointerEnter={updateSurfaceLight}
          onPointerLeave={hideSurfaceLight}
        >
          <div className="command-action-header">
            <div className="command-panel-eyebrow">
              Current Action
            </div>

            {actionComplete && (
              <div className="command-complete-badge">
                <Check
                  size={11}
                  strokeWidth={2}
                  aria-hidden="true"
                />
                Complete
              </div>
            )}
          </div>

          <motion.input
            type="text"
            className={[
              'command-next-action-input',
              actionComplete
                ? 'is-complete'
                : '',
            ]
              .filter(Boolean)
              .join(' ')}
            value={nextAction}
            placeholder="Continue JARVIS prototype"
            whileFocus={{
              scale: 1.006,
            }}
            transition={{
              duration: 0.16,
              ease: 'easeOut',
            }}
            onChange={(event) =>
              actions.setNextAction(
                event.target.value,
              )
            }
          />

          <div className="command-action-footer">
            <span>
              {actionComplete
                ? 'All steps cleared. Timer continues until time up.'
                : 'First unchecked checklist item drives the run.'}
            </span>
          </div>
        </article>

        <article
          className={[
            'command-panel',
            'command-panel-timer',
            statusTone,
          ]
            .filter(Boolean)
            .join(' ')}
          onPointerMove={updateSurfaceLight}
          onPointerEnter={updateSurfaceLight}
          onPointerLeave={hideSurfaceLight}
        >
          <div className="command-panel-heading-row">
            <div className="command-title-with-icon">
              <Timer
                className="command-section-icon"
                aria-hidden="true"
                size={15}
                strokeWidth={1.8}
              />

              <div className="command-panel-eyebrow">
                Timer
              </div>
            </div>
          </div>

          <div className="command-timer-value tabular-nums">
            <SevenSegmentTime
              value={timerText}
              className="command-seven-segment-time"
            />
          </div>

          {timer.status ===
            'idle' && (
            <div className="command-duration-custom">
              <label className="command-duration-field">
                <input
                  type="number"
                  min="0"
                  max="99"
                  step="1"
                  className="command-duration-input"
                  value={durationHours}
                  aria-label="Timer hours"
                  onFocus={(event) =>
                    event.target.select()
                  }
                  onChange={(event) => {
                    const hours =
                      Math.min(
                        99,
                        Math.max(
                          0,
                          Number.parseInt(
                            event.target.value ||
                              '0',
                            10,
                          ) || 0,
                        ),
                      )

                    actions.setDurationMinutes(
                      hours * 60 +
                        durationMinutesPart,
                    )
                  }}
                />

                <span className="command-duration-unit">
                  H
                </span>
              </label>

              <label className="command-duration-field">
                <input
                  type="number"
                  min="0"
                  max="59"
                  step="1"
                  className="command-duration-input"
                  value={durationMinutesPart}
                  aria-label="Timer minutes"
                  onFocus={(event) =>
                    event.target.select()
                  }
                  onChange={(event) => {
                    const minutes =
                      Math.min(
                        59,
                        Math.max(
                          0,
                          Number.parseInt(
                            event.target.value ||
                              '0',
                            10,
                          ) || 0,
                        ),
                      )

                    actions.setDurationMinutes(
                      durationHours * 60 +
                        minutes,
                    )
                  }}
                />

                <span className="command-duration-unit">
                  M
                </span>
              </label>
            </div>
          )}

          {timer.extensionCount > 0 && (
            <div className="command-auto-extend">
              AUTO EXTEND ×
              {timer.extensionCount}
            </div>
          )}

          <div className="command-duration-presets">
            {TIMER_PRESETS_MINUTES.map(
              (minutes) => (
                <motion.button
                  type="button"
                  key={minutes}
                  className={[
                    'command-duration-button',
                    selectedMinutes ===
                    minutes
                      ? 'is-selected'
                      : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  disabled={timerLocked}
                  whileTap={MICRO_TAP}
                  onClick={() =>
                    actions.setDurationMinutes(
                      minutes,
                    )
                  }
                >
                  {minutes}M
                </motion.button>
              ),
            )}
          </div>

          <div className="command-timer-actions">
            {timer.status ===
              'idle' && (
              <motion.button
                type="button"
                className="command-action-button primary"
                whileTap={MICRO_TAP}
                onClick={actions.start}
              >
                <Play
                  size={13}
                  fill="currentColor"
                  aria-hidden="true"
                />
                START
              </motion.button>
            )}

            {timer.status ===
              'active' && (
              <>
                <motion.button
                  type="button"
                  className="command-action-button primary"
                  whileTap={MICRO_TAP}
                  onClick={actions.pause}
                >
                  <Pause
                    size={13}
                    aria-hidden="true"
                  />
                  PAUSE
                </motion.button>

                <motion.button
                  type="button"
                  className="command-action-button subtle"
                  whileTap={MICRO_TAP}
                  onClick={actions.end}
                >
                  <Square
                    size={12}
                    aria-hidden="true"
                  />
                  END
                </motion.button>
              </>
            )}

            {timer.status ===
              'paused' && (
              <>
                <motion.button
                  type="button"
                  className="command-action-button primary"
                  whileTap={MICRO_TAP}
                  onClick={actions.resume}
                >
                  <Play
                    size={13}
                    fill="currentColor"
                    aria-hidden="true"
                  />
                  RESUME
                </motion.button>

                <motion.button
                  type="button"
                  className="command-action-button subtle"
                  whileTap={MICRO_TAP}
                  onClick={actions.end}
                >
                  <Square
                    size={12}
                    aria-hidden="true"
                  />
                  END
                </motion.button>
              </>
            )}

            {(timer.status ===
              'done' ||
              timer.status ===
                'ended') && (
              <motion.button
                type="button"
                className="command-action-button primary"
                whileTap={MICRO_TAP}
                onClick={
                  actions.prepareNewRun
                }
              >
                <RotateCcw
                  size={13}
                  aria-hidden="true"
                />
                NEW RUN
              </motion.button>
            )}
          </div>
        </article>

        <article
          className="command-panel command-panel-checklist"
          onPointerMove={updateSurfaceLight}
          onPointerEnter={updateSurfaceLight}
          onPointerLeave={hideSurfaceLight}
        >
          <div className="command-panel-heading-row">
            <div className="command-title-with-icon">
              <ListChecks
                className="command-section-icon"
                aria-hidden="true"
                size={15}
                strokeWidth={1.8}
              />

              <div className="command-panel-eyebrow">
                Checklist
              </div>
            </div>

            <div className="command-progress-label">
              {Math.round(
                progress * 100,
              )}%
            </div>
          </div>

          <div className="command-progress-track">
            <div
              className="command-progress-fill"
              style={{
                width: `${progress * 100}%`,
              }}
            />
          </div>

          <div className="command-checklist-items">
            {checklist.length ===
              0 && (
              <div className="command-empty-checklist">
                Add the first executable step.
              </div>
            )}

            {checklist.map(
              (item) => {
                const isCurrent =
                  item.id ===
                  currentItemId

                return (
                  <motion.div
                    key={item.id}
                    layout="position"
                    className={[
                      'command-check-item',
                      item.checked
                        ? 'is-done'
                        : '',
                      isCurrent
                        ? 'is-current'
                        : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    whileHover={{
                      x: item.checked
                        ? 0
                        : 2,
                    }}
                    transition={{
                      duration: 0.14,
                      ease: 'easeOut',
                    }}
                  >
                    <motion.button
                      type="button"
                      className="command-check-toggle"
                      whileTap={MICRO_TAP}
                      onClick={() =>
                        actions.toggleChecklistItem(
                          item.id,
                        )
                      }
                      disabled={
                        timer.status ===
                          'done' ||
                        timer.status ===
                          'ended'
                      }
                      aria-label={`Toggle ${item.text}`}
                    >
                      {item.checked && (
                        <Check
                          size={14}
                          strokeWidth={2.2}
                          aria-hidden="true"
                        />
                      )}

                      {!item.checked &&
                        isCurrent && (
                        <ChevronRight
                          size={15}
                          strokeWidth={2.1}
                          aria-hidden="true"
                        />
                      )}

                      {!item.checked &&
                        !isCurrent && (
                        <Circle
                          size={12}
                          strokeWidth={1.7}
                          aria-hidden="true"
                        />
                      )}
                    </motion.button>

                    <div className="command-check-content">
                      {isCurrent && (
                        <span className="command-current-label">
                          Now
                        </span>
                      )}

                      <span className="command-check-text">
                        {item.text}
                      </span>
                    </div>

                    <motion.button
                      type="button"
                      className="command-check-delete"
                      whileTap={MICRO_TAP}
                      onClick={() =>
                        actions.deleteChecklistItem(
                          item.id,
                        )
                      }
                      disabled={
                        timer.status ===
                          'done' ||
                        timer.status ===
                          'ended'
                      }
                      aria-label={`Delete ${item.text}`}
                    >
                      <X
                        size={13}
                        strokeWidth={1.9}
                        aria-hidden="true"
                      />
                    </motion.button>
                  </motion.div>
                )
              },
            )}
          </div>

          {timer.status !== 'done' &&
            timer.status !==
              'ended' && (
            <form
              className="command-add-step"
              onSubmit={addItem}
            >
              <input
                type="text"
                value={newItem}
                onChange={(event) =>
                  setNewItem(
                    event.target.value,
                  )
                }
                placeholder={
                  actionComplete &&
                  timer.status ===
                    'active'
                    ? 'Add next step while timer continues'
                    : 'Add checklist step'
                }
              />

              <motion.button
                type="submit"
                disabled={!newItem.trim()}
                whileTap={MICRO_TAP}
                aria-label="Add checklist step"
              >
                <Plus
                  size={15}
                  strokeWidth={2}
                  aria-hidden="true"
                />
              </motion.button>
            </form>
          )}
        </article>
      </div>

      <div className="command-external-module command-external-weather">
        <span>WEATHER</span>
        <strong>OFFLINE</strong>
      </div>

      <div className="command-external-module command-external-calendar">
        <span>CALENDAR</span>
        <strong>OFFLINE</strong>
      </div>

      <div className="command-external-module command-external-news">
        <span>NEWS</span>
        <strong>OFFLINE</strong>
      </div>
    </section>
  )
}
