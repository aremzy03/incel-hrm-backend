from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import TutorialProgressStatus, UserTutorialProgress

User = get_user_model()


class TutorialProgressTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="alice@example.com",
            password="testpass123",
        )
        self.other = User.objects.create_user(
            email="bob@example.com",
            password="testpass123",
        )
        self.url = reverse("tutorial-progress")

    def test_get_requires_auth(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_get_empty_list(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items"], [])

    def test_post_complete_creates_record(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.url,
            {"tour_id": "leave-employee", "action": "complete"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["tour_id"], "leave-employee")
        self.assertEqual(response.data["status"], TutorialProgressStatus.COMPLETED)
        self.assertEqual(
            UserTutorialProgress.objects.filter(user=self.user).count(),
            1,
        )

    def test_post_dismiss_upserts(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.url,
            {"tour_id": "loans-employee", "action": "complete"},
            format="json",
        )
        response = self.client.post(
            self.url,
            {"tour_id": "loans-employee", "action": "dismiss"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], TutorialProgressStatus.DISMISSED)

    def test_invalid_tour_id_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.url,
            {"tour_id": "unknown-tour", "action": "complete"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_action_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.url,
            {"tour_id": "leave-employee", "action": "reset"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_progress_scoped_to_authenticated_user(self):
        UserTutorialProgress.objects.create(
            user=self.other,
            tour_id="leave-employee",
            status=TutorialProgressStatus.COMPLETED,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.data["items"], [])
