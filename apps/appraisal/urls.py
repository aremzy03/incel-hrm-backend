from rest_framework.routers import DefaultRouter

from .views import (
    AppraisalCycleViewSet,
    AppraisalTemplateViewSet,
    AppraisalTypeViewSet,
    EmployeeAppraisalViewSet,
)

router = DefaultRouter()
router.register("appraisal-types", AppraisalTypeViewSet, basename="appraisal-type")
router.register("appraisal-cycles", AppraisalCycleViewSet, basename="appraisal-cycle")
router.register("appraisal-templates", AppraisalTemplateViewSet, basename="appraisal-template")
router.register("appraisals", EmployeeAppraisalViewSet, basename="appraisal")

urlpatterns = router.urls
