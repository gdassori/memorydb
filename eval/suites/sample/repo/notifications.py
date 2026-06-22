def send_notification(user, message):
    """Send a notification to a user via the queue."""
    return enqueue(message)


def enqueue(message):
    """Put a message on the delivery queue."""
    return message
