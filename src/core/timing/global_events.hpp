#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
// Canonical event facts and the derived numerical bundle index have different
// owners. The index contains offsets/identities, never another arc population.
void BindGlobalEvents(const std::vector<ArtifactRef>& inputs,const std::string& output);
void IndexGlobalEvents(const ArtifactRef& events,const std::string& output);
void ExportGlobalEvents(const std::vector<ArtifactRef>& inputs,const std::string& output);
void ImportGlobalMeasurements(const std::vector<ArtifactRef>& inputs,const std::string& output);
void AuditGlobalMeasurements(const std::vector<ArtifactRef>& inputs,const std::string& output);
void PublishGlobalQor(const std::vector<ArtifactRef>& inputs,const std::string& output);
void ValidateGlobalQor(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
