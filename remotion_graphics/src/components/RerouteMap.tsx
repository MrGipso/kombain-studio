import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps, Point} from '../types';
import {AnimatedLine, MapBackdrop, MovingDot} from './primitives';

const defaultRoute: Point[] = [
  {x: 420, y: 560, label: 'Вы'},
  {x: 720, y: 468},
  {x: 1040, y: 488},
  {x: 1400, y: 560, label: 'Сайт'},
];

const defaultAlternate: Point[] = [
  {x: 420, y: 560, label: 'Вы'},
  {x: 660, y: 660},
  {x: 1000, y: 720},
  {x: 1290, y: 660},
  {x: 1400, y: 560, label: 'Сайт'},
];

export const RerouteMap: React.FC<MotionGraphicProps> = ({
  title = 'Интернет ищет обходной путь',
  subtitle = 'Когда линия повреждена, трафик может уйти другим маршрутом',
  route = defaultRoute,
  alternateRoute = defaultAlternate,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const titleIn = spring({frame, fps, config: {damping: 18, stiffness: 86}});
  const breakIn = interpolate(frame, [fps * 2.3, fps * 2.9], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const altStart = Math.round(fps * 3.0);
  const altEnd = Math.round(fps * 5.8);
  const breakPoint = route[Math.floor(route.length / 2)] ?? {x: 960, y: 500};

  return (
    <div style={baseScene}>
      <MapBackdrop />
      <div style={{position: 'absolute', left: 0, right: 0, top: 76, textAlign: 'center', opacity: titleIn}}>
        <div style={{fontSize: 56, lineHeight: 1.05, fontWeight: 820}}>{title}</div>
        <div style={{fontSize: 28, color: palette.muted, marginTop: 18}}>{subtitle}</div>
      </div>
      <svg width="1920" height="1080" viewBox="0 0 1920 1080" style={{position: 'absolute', inset: 0}}>
        <AnimatedLine points={route} color={palette.danger} startFrame={12} endFrame={70} dashed opacity={0.7} />
        <g opacity={breakIn}>
          <circle cx={breakPoint.x} cy={breakPoint.y} r="38" fill="#ef444433" stroke={palette.danger} strokeWidth="5" />
          <line x1={breakPoint.x - 24} y1={breakPoint.y - 24} x2={breakPoint.x + 24} y2={breakPoint.y + 24} stroke={palette.danger} strokeWidth="7" strokeLinecap="round" />
          <line x1={breakPoint.x + 24} y1={breakPoint.y - 24} x2={breakPoint.x - 24} y2={breakPoint.y + 24} stroke={palette.danger} strokeWidth="7" strokeLinecap="round" />
        </g>
        <AnimatedLine points={alternateRoute} color={palette.accent} startFrame={altStart} endFrame={altEnd} />
        <MovingDot points={alternateRoute} color={palette.accent} startFrame={altStart} endFrame={altEnd} />
        {[route[0], route[route.length - 1]]
          .filter(Boolean)
          .map((point) => (
            <g key={point.label}>
              <circle cx={point.x} cy={point.y} r="16" fill={palette.panel} stroke={palette.accent} strokeWidth="5" />
              <text
                x={point.x}
                y={point.y + 48}
                fill={palette.text}
                fontSize="30"
                fontWeight="780"
                textAnchor="middle"
                fontFamily="Inter, Segoe UI, Arial, sans-serif"
              >
                {point.label}
              </text>
            </g>
          ))}
      </svg>
    </div>
  );
};
