import { Calculator } from './calculator';

describe('Calculator', () => {
  it('adds two positive numbers', () => {
    expect(new Calculator().add(2, 3)).toBe(5);
  });

  it('recognises adulthood at the boundary', () => {
    expect(new Calculator().isAdult(18)).toBe(true);
  });

  it('rejects minors', () => {
    expect(new Calculator().isAdult(17)).toBe(false);
  });
});
