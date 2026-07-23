from locust import HttpUser, task, between

class APIUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def generate_report(self):
        self.client.post(
            "/generate_report",
            json={
                "firstname": "John",
                "lastname": "Doe",
                "email": "johndoe@example.com",
            }
        )