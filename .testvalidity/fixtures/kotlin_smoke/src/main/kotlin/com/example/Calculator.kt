package com.example

class Calculator {
    fun add(a: Int, b: Int): Int {
        return a + b
    }

    fun isAdult(age: Int): Boolean {
        if (age >= 18 && age < 200) {
            return true
        }
        return false
    }
}
