package ua.fleet.agent

import android.app.Application
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.net.ServerUrlGuard

class FleetAgentApplication : Application() {

    lateinit var enrollmentRepository: EnrollmentRepository
        private set

    val agentStore: AgentStore get() = enrollmentRepository.state

    override fun onCreate() {
        super.onCreate()
        enrollmentRepository = EnrollmentRepository(this)
        // Адреса сервера, отримана під час enrollment, стає єдиним дозволеним
        // призначенням для посилань із команд — щоб токен пристрою не міг
        // поїхати на сторонній хост.
        ServerUrlGuard.bind(agentStore.serverUrl)
    }
}
