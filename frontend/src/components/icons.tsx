/** Authored SVG icon set — one consistent 1.6 stroke, currentColor. */

interface IconProps {
  size?: number;
}

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
});

export const IconUpload = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M12 16V4M12 4l-4 4M12 4l4 4" />
    <path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16" />
  </svg>
);

export const IconLink = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M10 13.5a3.5 3.5 0 0 0 5 0l3-3a3.54 3.54 0 0 0-5-5l-1 1" />
    <path d="M14 10.5a3.5 3.5 0 0 0-5 0l-3 3a3.54 3.54 0 0 0 5 5l1-1" />
  </svg>
);

export const IconDownload = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M12 4v12M12 16l-4-4M12 16l4-4" />
    <path d="M4 18v1.5A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5V18" />
  </svg>
);

export const IconPlay = ({ size = 20 }: IconProps) => (
  <svg {...base(size)} fill="currentColor" stroke="none">
    <path d="M8 5.5v13l10.5-6.5z" />
  </svg>
);

export const IconScissors = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <circle cx="6" cy="6" r="2.4" />
    <circle cx="6" cy="18" r="2.4" />
    <path d="M8 7.4 20 18M8 16.6 20 6" />
  </svg>
);

export const IconSpinner = ({ size = 20 }: IconProps) => (
  <svg {...base(size)} className="spin">
    <path d="M12 3a9 9 0 1 0 9 9" />
  </svg>
);

export const IconCheck = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M4 12.5 9.5 18 20 6.5" />
  </svg>
);

export const IconWarn = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M12 3.5 21.5 20h-19z" />
    <path d="M12 9.5v4.5M12 17h.01" />
  </svg>
);

export const IconArrow = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export const IconClose = ({ size = 20 }: IconProps) => (
  <svg {...base(size)}>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);
