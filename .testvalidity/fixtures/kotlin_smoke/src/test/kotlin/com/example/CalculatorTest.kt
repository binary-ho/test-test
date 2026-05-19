package com.example

import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.assertFalse

class CalculatorTest {
    @Test
    fun shouldAddTwoNumbers() {
        assertEquals(5, Calculator().add(2, 3))
    }

    @Test
    fun shouldRecognizeAdultAt18() {
        assertTrue(Calculator().isAdult(18))
    }

    @Test
    fun shouldRejectMinor() {
        assertFalse(Calculator().isAdult(17))
    }
}
