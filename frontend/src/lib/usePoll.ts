import { useState, useEffect, useCallback } from 'react'

export function usePoll(intervalMs = 30_000) {
  const [tick, setTick] = useState(0)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(() => new Date())

  const bump = () => {
    setTick(t => t + 1)
    setLastUpdated(new Date())
  }

  const refresh = useCallback(bump, [])

  useEffect(() => {
    const id = setInterval(bump, intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])

  return { tick, refresh, lastUpdated }
}

export function fmtUpdated(d: Date | null): string {
  if (!d) return ''
  return `Updated ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
}
