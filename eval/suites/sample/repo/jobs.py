from notifications import send_notification


class MassNotificationJob:
    """Triggers mass notifications."""

    def run(self, users):
        for user in users:
            send_notification(user, "hello")
