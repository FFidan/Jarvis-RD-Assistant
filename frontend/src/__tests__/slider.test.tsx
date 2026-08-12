import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Slider } from '@/components/ui/slider';

describe('Slider accessible name', () => {
  it('names the element that actually carries role=slider', () => {
    // Radix renders role="slider" on the thumb, not the root, and the thumb
    // takes its name only from its own props. A name left on the root reads as
    // unlabelled to a screen reader even though getByLabelText still finds it.
    render(<Slider aria-label="Reading window" min={0} max={10} value={[5]} />);
    expect(screen.getByRole('slider')).toHaveAccessibleName('Reading window');
  });
});
