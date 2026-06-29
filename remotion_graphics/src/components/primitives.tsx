import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {palette} from '../style';
import type {Point} from '../types';

export const pointsToPath = (points: Point[]) => {
  if (points.length === 0) {
    return '';
  }
  if (points.length === 1) {
    return `M ${points[0].x} ${points[0].y}`;
  }
  const [first, ...rest] = points;
  return `M ${first.x} ${first.y} ${rest.map((point) => `L ${point.x} ${point.y}`).join(' ')}`;
};

export const pathDistance = (points: Point[]) => {
  let distance = 0;
  for (let index = 1; index < points.length; index++) {
    const previous = points[index - 1];
    const current = points[index];
    distance += Math.hypot(current.x - previous.x, current.y - previous.y);
  }
  return Math.max(distance, 1);
};

export const interpolatePoint = (points: Point[], progress: number): Point => {
  if (points.length === 0) {
    return {x: 0, y: 0};
  }
  if (points.length === 1) {
    return points[0];
  }
  const total = pathDistance(points);
  let target = total * progress;
  for (let index = 1; index < points.length; index++) {
    const previous = points[index - 1];
    const current = points[index];
    const segment = Math.hypot(current.x - previous.x, current.y - previous.y);
    if (target <= segment) {
      const local = segment === 0 ? 0 : target / segment;
      return {
        x: previous.x + (current.x - previous.x) * local,
        y: previous.y + (current.y - previous.y) * local,
      };
    }
    target -= segment;
  }
  return points[points.length - 1];
};

export const AnimatedLine: React.FC<{
  points: Point[];
  color?: string;
  width?: number;
  startFrame?: number;
  endFrame?: number;
  dashed?: boolean;
  opacity?: number;
}> = ({points, color = palette.accent, width = 7, startFrame = 0, endFrame = 80, dashed = false, opacity = 1}) => {
  const frame = useCurrentFrame();
  const length = pathDistance(points);
  const progress = interpolate(frame, [startFrame, endFrame], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <path
      d={pointsToPath(points)}
      fill="none"
      stroke={color}
      strokeWidth={width}
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeDasharray={dashed ? '18 18' : length}
      strokeDashoffset={dashed ? 0 : length * (1 - progress)}
      opacity={opacity}
      style={{filter: `drop-shadow(0 0 16px ${color}77)`}}
    />
  );
};

export const MovingDot: React.FC<{
  points: Point[];
  color?: string;
  startFrame?: number;
  endFrame?: number;
}> = ({points, color = palette.accent, startFrame = 0, endFrame = 80}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = interpolate(frame, [startFrame, endFrame], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const point = interpolatePoint(points, progress);
  const pulse = interpolate(Math.sin(frame / (fps / 3)), [-1, 1], [0.76, 1.12]);
  return (
    <g transform={`translate(${point.x} ${point.y}) scale(${pulse})`}>
      <circle r="20" fill={color} opacity="0.24" />
      <circle r="10" fill={color} />
      <circle r="4" fill="#ffffff" />
    </g>
  );
};

export const MapBackdrop: React.FC = () => {
  return (
    <svg width="1920" height="1080" viewBox="0 0 1920 1080" style={{position: 'absolute', inset: 0}}>
      <path
        d="M188 455 C260 350 420 330 542 394 C660 456 744 374 865 406 C1018 446 1052 568 1212 516 C1356 470 1488 530 1596 636 C1410 706 1198 716 1024 668 C860 622 746 702 566 666 C402 632 266 566 188 455 Z"
        fill="#172033"
        opacity="0.72"
      />
      <path
        d="M1034 316 C1126 254 1272 262 1392 328 C1508 394 1588 392 1692 364 C1660 464 1546 516 1410 496 C1264 474 1168 406 1034 316 Z"
        fill="#172033"
        opacity="0.62"
      />
      <path
        d="M310 690 C474 632 650 728 842 738 C1030 748 1196 680 1378 742 C1228 820 1056 860 836 844 C630 828 448 790 310 690 Z"
        fill="#172033"
        opacity="0.55"
      />
      <g opacity="0.16" stroke="#94a3b8" strokeWidth="1">
        {Array.from({length: 9}).map((_, index) => (
          <line key={`v-${index}`} x1={240 + index * 180} y1="250" x2={240 + index * 180} y2="850" />
        ))}
        {Array.from({length: 5}).map((_, index) => (
          <line key={`h-${index}`} x1="180" y1={300 + index * 120} x2="1740" y2={300 + index * 120} />
        ))}
      </g>
    </svg>
  );
};
