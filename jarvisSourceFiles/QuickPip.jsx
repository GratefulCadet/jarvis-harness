import {
  useEffect,
  useRef,
  useState,
} from 'react'

import {
  AnimatePresence,
  motion,
} from 'motion/react'

import {
  TIMER_PRESETS_MINUTES,
} from './useExecutionSession'

import SevenSegmentTime from './SevenSegmentTime'

export default function QuickPip({
  execution,
}) {
  const {
    nextAction,
    timer,
    timerText,
    actionComplete,
    currentItem,
    actions,
  } = execution

  const [
    completedFeedback,
    setCompletedFeedback,
  ] = useState(null)

  const [
    ambientIndex,
    setAmbientIndex,
  ] = useState(0)

  const feedbackTimerRef =
    useRef(null)

  useEffect(() => {
    return () => {
      if (
        feedbackTimerRef.current
      ) {
        window.clearTimeout(
          feedbackTimerRef.current,
        )
      }
    }
  }, [])

  useEffect(() => {
    const intervalId =
      window.setInterval(
        () => {
          setAmbientIndex(
            (index) =>
              (index + 1) % 4,
          )
        },
        2600,
      )

    return () => {
      window.clearInterval(
        intervalId,
      )
    }
  }, [])

    const selectedMinutes =
    Math.round(
        timer.originalDurationMs /
        60_000,
    )

    const isPresetDuration =
    TIMER_PRESETS_MINUTES.includes(
        selectedMinutes,
    )

    const customDurationHours =
    Math.floor(
        selectedMinutes / 60,
    )

    const customDurationMinutes =
    selectedMinutes % 60

    const customDurationLabel = [
    customDurationHours > 0
        ? `${customDurationHours}h`
        : '',
    customDurationMinutes > 0
        ? `${customDurationMinutes}m`
        : '',
    ]
    .filter(Boolean)
    .join(' ')

  const handleCompleteCurrentItem =
    () => {
      if (!currentItem) {
        return
      }

      setCompletedFeedback(
        currentItem.text,
      )

      actions.toggleChecklistItem(
        currentItem.id,
      )

      if (
        feedbackTimerRef.current
      ) {
        window.clearTimeout(
          feedbackTimerRef.current,
        )
      }

      feedbackTimerRef.current =
        window.setTimeout(
          () => {
            setCompletedFeedback(
              null,
            )
          },
          420,
        )
    }

  const visibleStepText =
    completedFeedback ||
    currentItem?.text ||
    'Current action complete'

  const stepIsComplete =
    Boolean(
      completedFeedback,
    ) || !currentItem

  const ambientItems = [
    {
      label: 'WEATHER',
      text: 'Clear focus window',
    },
    {
      label: 'EXECUTION',
      text: `${visibleStepText} · ${timerText}`,
    },
    {
      label: 'NOTE',
      text: 'Keep the next move small',
    },
    {
      label: 'TODAY',
      text: 'Review priority after this run',
    },
  ]

  const ambientItem =
    ambientItems[
      ambientIndex %
        ambientItems.length
    ]

  return (
    <aside
      className="quick-pip"
      aria-label="JARVIS quick interaction"
    >
      <div className="quick-pip-next-action">
        <div className="quick-pip-text-loop">
          <span className="quick-pip-text-loop-label">
            {ambientItem.label}
          </span>

          <AnimatePresence
            initial={false}
            mode="wait"
          >
            <motion.span
              key={`${ambientItem.label}-${ambientItem.text}`}
              className="quick-pip-text-loop-value"
              initial={{
                opacity: 0,
                y: 6,
                filter: 'blur(3px)',
              }}
              animate={{
                opacity: 1,
                y: 0,
                filter: 'blur(0px)',
              }}
              exit={{
                opacity: 0,
                y: -5,
                filter: 'blur(3px)',
              }}
              transition={{
                duration: 0.24,
                ease: [
                  0.22,
                  1,
                  0.36,
                  1,
                ],
              }}
            >
              {ambientItem.text}
            </motion.span>
          </AnimatePresence>
        </div>

        <span className="quick-pip-kicker">
          {actionComplete
            ? 'NEXT ACTION : COMPLETE'
            : 'NEXT ACTION'}
        </span>

        <span
          className={[
            'quick-pip-action-text',
            actionComplete
              ? 'is-complete'
              : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          {nextAction ||
            'Continue JARVIS prototype'}
        </span>
      </div>


      <div
        className="quick-pip-timer"
        aria-label="Focus timer"
      >
        <SevenSegmentTime
          value={timerText}
        />

        {timer.status ===
          'idle' && (
          <select
  className="quick-pip-duration-select"
  value={
    isPresetDuration
      ? String(
          selectedMinutes,
        )
      : 'custom'
  }
  onChange={(event) => {
    if (
      event.target.value ===
      'custom'
    ) {
      return
    }

    actions.setDurationMinutes(
      event.target.value,
    )
  }}
  aria-label="Timer duration"
>
  {!isPresetDuration && (
    <option value="custom">
      {customDurationLabel ||
        'Custom'}
    </option>
  )}

  {TIMER_PRESETS_MINUTES.map(
    (minutes) => (
      <option
        key={minutes}
        value={minutes}
      >
        {minutes} min
      </option>
    ),
  )}
</select>
        )}

        <div className="quick-pip-timer-actions">
          {timer.status ===
            'idle' && (
            <button
              type="button"
              className="quick-pip-mini-button"
              onClick={actions.start}
            >
              <motion.span
                layout
              >
                START
              </motion.span>
            </button>
          )}

          {timer.status ===
            'active' && (
            <button
              type="button"
              className="quick-pip-mini-button quick-pip-pause-resume-button"
              onClick={actions.pause}
            >
              <motion.span
                layout
              >
                PAUSE
              </motion.span>
            </button>
          )}

          {timer.status ===
            'paused' && (
            <button
              type="button"
              className="quick-pip-mini-button quick-pip-pause-resume-button"
              onClick={actions.resume}
            >
              <motion.span
                layout
              >
                RESUME
              </motion.span>
            </button>
          )}

          {(timer.status ===
            'done' ||
            timer.status ===
              'ended') && (
            <span className="quick-pip-open-main-hint">
                Open JARVIS to continue
            </span>
          )}
        </div>
      </div>

      <div
        className={[
          'quick-pip-current-step',
          stepIsComplete
            ? 'is-complete'
            : '',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        <button
          type="button"
          className="quick-pip-current-step-toggle"
          onClick={
            handleCompleteCurrentItem
          }
          disabled={
            !currentItem ||
            Boolean(
              completedFeedback,
            ) ||
            timer.status ===
              'done' ||
            timer.status ===
              'ended'
          }
          aria-label={
            currentItem
              ? `Complete ${currentItem.text}`
              : 'Current action complete'
          }
        >
          {stepIsComplete
            ? '✓'
            : '○'}
        </button>

        <div className="quick-pip-current-step-copy">
            <span className="quick-pip-current-step-label">
            {stepIsComplete && (
            <span className="quick-pip-current-step-label">
            CLEARED
        </span>
)}
          </span>

          <span className="quick-pip-current-step-text">
            {visibleStepText}
          </span>
        </div>
      </div>
    </aside>
  )
}
