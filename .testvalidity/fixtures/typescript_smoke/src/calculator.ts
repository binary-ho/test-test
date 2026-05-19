export class Calculator {
  add(a: number, b: number): number {
    return a + b;
  }

  isAdult(age: number): boolean {
    if (age >= 18 && age < 200) {
      return true;
    }
    return false;
  }
}
