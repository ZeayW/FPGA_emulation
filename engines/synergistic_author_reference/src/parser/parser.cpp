#include "parser.h"

#include <dirent.h>

/*输入指定文件夹路径，建立database*/
void buildDatabase(const char *folderPath, database::Database &database) {
  std::cout << "Start Loading data form " << folderPath << std::endl;
  auto                                      &netlist = database.netlist();
  auto                                      &nodes   = database.nodes();
  auto                                      &dies    = database.dies();

  std::string                                line;

  std::unordered_map<std::string, IndexType> die_name_to_id;
  std::unordered_map<std::string, IndexType> node_name_to_id;
  std::unordered_map<std::string, IndexType> die_name_to_fpga_id;

  DIR                                       *dir;
  struct dirent                             *entry;
  std::vector<std::string>                   filenames;

  if ((dir = opendir(folderPath)) != nullptr) {
    while ((entry = readdir(dir)) != nullptr) {
      if (entry->d_type == DT_REG) {   // 仅处理常规文件
        std::string fileName = entry->d_name;
        filenames.push_back(fileName);
      }
    }
    closedir(dir);

    std::sort(filenames.begin(), filenames.end());
    std::unordered_map<IndexType, IndexType> node_name_to_id;
    for (const std::string &fileName : filenames) {
      std::string filePath = folderPath;
      filePath += "/";
      filePath += fileName;

      if (fileName == "design.fpga.out") {
        std::cout << "Loading design.fpga.out" << std::endl;
        std::ifstream ifs(filePath);
        std::string token;
        IndexType fpga_id;
        while (ifs >> token) {
          if (token[0] == 'F') {
            fpga_id = database.dies().size();
            database.dies().emplace_back(database.dies().size());
            database.die(fpga_id).setfpgaId(fpga_id);
            database.die(fpga_id).setname(token.substr(0, token.size() - 1));
          }  else {
            IndexType node_id = std::stoi(token.substr(1));
            IndexType idx = database.nodes().size();
            database.nodes().emplace_back(idx);
            database.node(idx).setDieId(fpga_id);
            node_name_to_id[node_id] = idx;
          }
        }
        ifs.close();
      }
      if (fileName == "design.info") {
        std::cout << "Loading design.info" << std::endl;
        std::ifstream ifs(filePath);
        std::string fpga_name;
        IndexType cap;
        while (ifs >> fpga_name >> cap) {
          for (auto& die : database.dies()) {
            if (die.name() == fpga_name) {
              die.setCap(cap);
            }
          }
        }
        ifs.close();
      }
      if (fileName == "design.net") {
        std::cout << "Loading design.net" << std::endl;
        std::ifstream ifs(filePath);
        std::string data;
        while (getline(ifs, data)) {
          IndexType net_idx = database.netlist().size();
          database.netlist().emplace_back(net_idx);
          std::istringstream iss(data);
          std::string token;
          iss >> token;
          database.net(net_idx).setNodeIds_s(node_name_to_id[std::stoi(token.substr(1))]);
          iss >> token;
          while (iss >> token) {
          database.net(net_idx).setNodeIds_l(node_name_to_id[std::stoi(token.substr(1))]);
          }
        }
        std::cout << "Successfully load " << database.netlist().size() << " nets!" << std::endl; 
        ifs.close();
      }
      if (fileName == "design.topo") {
        std::cout << "Loading design.topo" << std::endl;
        FILE* file = fopen(filePath.c_str(), "r");
        char s[100];
        std::vector<std::vector<IndexType> > cap;
        cap.resize(database.dies().size());
        for (IndexType i = 0; i < database.dies().size(); i++) {
          fscanf(file, " %[^:]: ", s);
          IndexType fpga_id = -1;
          for (IndexType j = 0; j < database.dies().size(); j++) {
            if (database.die(j).name() == std::string(s)) {
              fpga_id = j;
              break;
            }
          }
          if (fpga_id == -1) {
            std::cout << "[ERROR] FPGA " << s << " Not Found!\n";
            exit(-1);
          }
            IndexType idx;
          for (IndexType j = 1; j < database.dies().size(); j++) {
            fscanf(file, "%d,", &idx);
            cap[fpga_id].push_back(idx);
          }       
          fscanf(file, "%d", &idx);
          cap[fpga_id].push_back(idx);
        }
        database.setnetworks(cap);
        fclose(file);
      }
    }
  } else {
    std::cout << "不存在的文件夹" << std::endl;
  }
  std::cout << "Load Complete!" << std::endl;
}