import React from 'react';
import {AbsoluteFill} from 'remotion';
import {getTemplate} from './templateRegistry';
import type {MotionGraphicProps} from './types';

const errorStyle: React.CSSProperties = {
  background: '#0b1020',
  color: '#ef4444',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 32,
  fontFamily: 'monospace',
};

export const MotionGraphic: React.FC<MotionGraphicProps> = (props) => {
  const Component = getTemplate(props.template);

  if (!Component) {
    return (
      <AbsoluteFill style={errorStyle}>
        Unknown template: {props.template}
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill>
      <Component {...props} />
    </AbsoluteFill>
  );
};
