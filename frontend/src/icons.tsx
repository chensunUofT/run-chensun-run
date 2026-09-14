import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

const shared = {
  fill: 'none',
  stroke: 'currentColor',
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  strokeWidth: 1.8,
}

export function Icon({ name, size = 20, ...props }: IconProps & { name: IconName }) {
  const common = { ...shared, width: size, height: size, viewBox: '0 0 24 24', ...props }

  switch (name) {
    case 'activity':
      return <svg {...common}><path d="M3 12h4l2.2-6 4.2 12 2.2-6H21" /></svg>
    case 'chart':
      return <svg {...common}><path d="M4 19V5M4 19h16" /><path d="m7 15 3-4 3 2 5-7" /><circle cx="18" cy="6" r="1" fill="currentColor" stroke="none" /></svg>
    case 'shoe':
      return <svg {...common}><path d="M4.5 5.5c1.7 1.6 2.9 3.1 4.4 5.5l2.2 3.4c.9 1.4 2.4 2.1 4 2.1H20c.5 0 .9.4.9.9v.5c0 .6-.4 1.1-1.1 1.1H6.2A3.2 3.2 0 0 1 3 15.8V8.5c0-1.7.5-2.7 1.5-3Z" /><path d="m8.9 11 2.6-.6 2.1 1.1M6.7 8.7l2.5-.5" /></svg>
    case 'sun':
      return <svg {...common}><circle cx="12" cy="12" r="3.5" /><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" /></svg>
    case 'settings':
      return <svg {...common}><circle cx="12" cy="12" r="3" /><path d="m19.4 15 .1.1a1.8 1.8 0 0 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3 .8v.2a1.8 1.8 0 0 1-3.6 0v-.2a1.8 1.8 0 0 0-3-.8l-.1.1a1.8 1.8 0 0 1-2.5-2.5l.1-.1a1.8 1.8 0 0 0-.8-3h-.2a1.8 1.8 0 0 1 0-3.6H4a1.8 1.8 0 0 0 .8-3l-.1-.1a1.8 1.8 0 0 1 2.5-2.5l.1.1a1.8 1.8 0 0 0 3-.8V4a1.8 1.8 0 0 1 3.6 0v.2a1.8 1.8 0 0 0 3 .8l.1-.1a1.8 1.8 0 0 1 2.5 2.5l-.1.1a1.8 1.8 0 0 0 .8 3h.2a1.8 1.8 0 0 1 0 3.6h-.2a1.8 1.8 0 0 0-.8.9Z" /></svg>
    case 'plus':
      return <svg {...common}><path d="M12 5v14M5 12h14" /></svg>
    case 'arrow-right':
      return <svg {...common}><path d="M4 12h15M13 6l6 6-6 6" /></svg>
    case 'arrow-up-right':
      return <svg {...common}><path d="M5 19 19 5M9 5h10v10" /></svg>
    case 'chevron-down':
      return <svg {...common}><path d="m6 9 6 6 6-6" /></svg>
    case 'chevron-left':
      return <svg {...common}><path d="m15 18-6-6 6-6" /></svg>
    case 'chevron-right':
      return <svg {...common}><path d="m9 18 6-6-6-6" /></svg>
    case 'edit':
      return <svg {...common}><path d="m4 16.5-.8 3.3 3.3-.8L18.2 7.3a2.3 2.3 0 0 0-3.3-3.3L4 16.5Z" /><path d="m13.5 5.5 3 3" /></svg>
    case 'trash':
      return <svg {...common}><path d="M5 7h14M10 11v5M14 11v5M8 7l.7-2h6.6l.7 2M7 7l.7 13h8.6L17 7" /></svg>
    case 'close':
      return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>
    case 'download':
      return <svg {...common}><path d="M12 3v12M7 10l5 5 5-5M4 20h16" /></svg>
    case 'upload':
      return <svg {...common}><path d="M12 15V3M7 8l5-5 5 5M4 20h16" /></svg>
    case 'calendar':
      return <svg {...common}><rect x="3.5" y="5" width="17" height="15" rx="2" /><path d="M7 3v4M17 3v4M3.5 9h17" /></svg>
    case 'clock':
      return <svg {...common}><circle cx="12" cy="12" r="8.5" /><path d="M12 7v5l3.5 2" /></svg>
    case 'heart':
      return <svg {...common}><path d="M20.5 8.8c0 4.8-8.5 10.2-8.5 10.2S3.5 13.6 3.5 8.8A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 8.5 1.6Z" /></svg>
    case 'zap':
      return <svg {...common}><path d="m13 2-8 12h6l-1 8 8-12h-6l1-8Z" /></svg>
    case 'database':
      return <svg {...common}><ellipse cx="12" cy="5" rx="7.5" ry="2.8" /><path d="M4.5 5v7c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V5M4.5 12v7c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-7" /></svg>
    case 'file':
      return <svg {...common}><path d="M6 3.5h7l5 5V20H6z" /><path d="M13 3.5V9h5M9 13h6M9 16h6" /></svg>
    case 'info':
      return <svg {...common}><circle cx="12" cy="12" r="8.5" /><path d="M12 10.5v5M12 7.5h.01" /></svg>
    case 'refresh':
      return <svg {...common}><path d="M19 8a7.5 7.5 0 0 0-13.2-2L4 8M4 4v4h4M5 16a7.5 7.5 0 0 0 13.2 2L20 16m0 4v-4h-4" /></svg>
    case 'external':
      return <svg {...common}><path d="M14 4h6v6M20 4l-9 9" /><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5" /></svg>
    case 'menu':
      return <svg {...common}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
    case 'check':
      return <svg {...common}><path d="m5 12 4 4L19 6" /></svg>
    case 'spark':
      return <svg {...common}><path d="m12 2 1.5 6.5L20 10l-6.5 1.5L12 18l-1.5-6.5L4 10l6.5-1.5L12 2Z" /><path d="m19 16 .5 2.5L22 19l-2.5.5L19 22l-.5-2.5L16 19l2.5-.5L19 16Z" /></svg>
    default:
      return <svg {...common}><circle cx="12" cy="12" r="8" /></svg>
  }
}

export type IconName =
  | 'activity'
  | 'chart'
  | 'shoe'
  | 'sun'
  | 'settings'
  | 'plus'
  | 'arrow-right'
  | 'arrow-up-right'
  | 'chevron-down'
  | 'chevron-left'
  | 'chevron-right'
  | 'edit'
  | 'trash'
  | 'close'
  | 'download'
  | 'upload'
  | 'calendar'
  | 'clock'
  | 'heart'
  | 'zap'
  | 'database'
  | 'file'
  | 'info'
  | 'refresh'
  | 'external'
  | 'menu'
  | 'check'
  | 'spark'
