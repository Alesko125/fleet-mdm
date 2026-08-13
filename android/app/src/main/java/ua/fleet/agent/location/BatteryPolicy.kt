package ua.fleet.agent.location

/**
 * Ощадливе ставлення до батареї.
 *
 * Чиста логіка без залежностей від Android — тому легко тестується на JVM.
 * Правила прості й передбачувані: що нижчий заряд, то рідше й грубіше
 * визначаємо позицію; на зарядці працюємо в штатному режимі.
 */
object BatteryPolicy {

    const val CRITICAL_PERCENT = 10
    const val LOW_PERCENT = 20
    const val REDUCED_PERCENT = 35

    /** Чи збирати координати взагалі. */
    fun shouldCollect(batteryPercent: Int, charging: Boolean): Boolean =
        charging || batteryPercent > CRITICAL_PERCENT

    /** Фактичний інтервал: множник залежить від заряду. */
    fun effectiveIntervalSeconds(
        baseSeconds: Int,
        batteryPercent: Int,
        charging: Boolean,
    ): Int {
        if (charging) return baseSeconds
        val multiplier = when {
            batteryPercent <= LOW_PERCENT -> 3
            batteryPercent <= REDUCED_PERCENT -> 2
            else -> 1
        }
        return baseSeconds * multiplier
    }

    /** Висока точність (GPS) вимикається на низькому заряді. */
    fun useHighAccuracy(requested: Boolean, batteryPercent: Int, charging: Boolean): Boolean =
        requested && (charging || batteryPercent > LOW_PERCENT)

    /** Людяне пояснення для екрана агента. */
    fun describe(batteryPercent: Int, charging: Boolean): String = when {
        charging -> "штатний режим (заряджається)"
        batteryPercent <= CRITICAL_PERCENT -> "збір призупинено: заряд ≤ $CRITICAL_PERCENT%"
        batteryPercent <= LOW_PERCENT -> "рідше в 3 рази, без GPS: заряд ≤ $LOW_PERCENT%"
        batteryPercent <= REDUCED_PERCENT -> "рідше вдвічі: заряд ≤ $REDUCED_PERCENT%"
        else -> "штатний режим"
    }
}
